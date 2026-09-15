#!/usr/bin/env python
"""Private archive helpers for backup.sh and restore.sh; no container operations."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit

import yaml

from deployment import load_deployment, validate_deployment, verify_configuration

DEPLOY_FILES = ('deploy/compose.yaml', 'deploy/compose.offline.yaml', 'deploy/Caddyfile')
APP_SECRETS = {'headscale_oidc_secret', 'headplane_oidc_secret', 'headscale_api_key', 'cookie_secret'}


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def validate_effective_config(deployment, files):
    """Ensure hashed Compose files agree with the declared ownership boundary."""
    expected = set(deployment['services'])
    external = deployment['identity']['mode'] == 'external'
    for name in DEPLOY_FILES[:2]:
        config = yaml.safe_load(files[name])
        if not isinstance(config, dict) or not isinstance(config.get('services'), dict):
            raise ValueError('Effective Compose services must be a mapping: ' + name)
        actual = set(config['services'])
        if (name == 'deploy/compose.yaml' and actual != expected) or not actual <= expected:
            raise ValueError('Effective Compose services differ from deployment descriptor: ' + name)
        if external:
            if config.get('secrets') or set(config.get('volumes') or {}) - {'caddy_data', 'caddy_config'}:
                raise ValueError('External Compose contains identity-owned secrets or volumes')
    if external:
        caddy = files['deploy/Caddyfile']
        if isinstance(caddy, bytes):
            caddy = caddy.decode()
        # The shared issuer owns its hostname and proxy route.
        host = urlsplit(deployment['identity']['issuer']).hostname
        if host in caddy or 'CASDOOR_HOST' in caddy or 'casdoor:8000' in caddy:
            raise ValueError('External proxy cannot route the shared identity hostname')


def runtime_deployment(runtime):
    path = runtime / 'deployment.json'
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise ValueError('Deployment descriptor cannot be a symlink')
        deployment = load_deployment(path)
        if set(deployment['configuration_sha256']) != set(DEPLOY_FILES):
            raise ValueError('Deployment descriptor must bind all effective deploy files')
        verify_configuration(runtime, deployment)
        validate_effective_config(deployment, {name: (runtime / name).read_bytes() for name in DEPLOY_FILES})
        return deployment, True
    if (runtime / 'deploy').exists():
        raise ValueError('Effective deploy files require deployment.json')
    return load_deployment(), False


def inspect_runtime(runtime, source):
    values = dict(line.split('=', 1) for line in (runtime / 'compose.env').read_text().splitlines() if '=' in line)
    if values.get('RUNTIME_DIR') != str(runtime):
        raise ValueError('compose.env RUNTIME_DIR differs from the requested backup source')
    for path in runtime.rglob('*'):
        if path.relative_to(runtime).as_posix() == 'headscale/data/headscale.sock':
            continue
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('Runtime contains a link or special file that cannot be safely restored: ' + str(path))
    deployment, explicit = runtime_deployment(runtime)
    compose = (runtime if explicit else source) / 'deploy/compose.yaml'
    return deployment, explicit, compose


def worker_secrets(config):
    """Select only credentials referenced by the local worker configuration."""
    names = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('_file') and isinstance(item, str) and item.startswith('/run/secrets/'):
                    path = PurePosixPath(item)
                    if len(path.parts) != 4 or '..' in path.parts or path.name == 'db_password':
                        raise ValueError('Worker secret must name a Tailnet-owned file directly under /run/secrets')
                    names.add(path.name)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(config)
    return names


def external_path_allowed(relative, local_worker, secrets):
    parts = PurePosixPath(relative).parts
    if not parts:
        return True
    if parts[0] in ('headscale', 'headplane', 'deploy'):
        return True
    if parts[0] == 'sync':
        return local_worker
    if parts[0] == 'secrets':
        return len(parts) == 1 or (len(parts) == 2 and parts[1] in secrets)
    return len(parts) == 1 and parts[0] in ('compose.env', 'deployment.json')


def stage_backup(runtime, stage, source):
    deployment, explicit, _ = inspect_runtime(runtime, source)
    bundled = deployment['identity']['mode'] == 'bundled'
    local_worker = deployment['directory_sync']['owner'] == 'local'
    config = runtime / 'sync/sync.json'
    secret_names = APP_SECRETS | (worker_secrets(json.loads(config.read_text())) if local_worker and config.exists() else set())
    target = stage / 'runtime'
    target.mkdir()
    for path in sorted(runtime.rglob('*')):
        relative = path.relative_to(runtime)
        if relative.as_posix() in ('.maintenance.lock', 'headscale/data/headscale.sock'):
            continue
        if not bundled and not external_path_allowed(relative.as_posix(), local_worker, secret_names):
            continue
        # Non-local workers are omitted even when the identity service is bundled.
        if not local_worker and relative.parts[0] == 'sync':
            continue
        if not local_worker and relative.parts[0] == 'secrets' and len(relative.parts) == 2:
            allowed = APP_SECRETS | ({'db_password'} if bundled else set())
            if relative.name not in allowed:
                continue
        output = target / relative
        if path.is_dir():
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(path, output)
            output.chmod(0o600)
    if not explicit:
        (target / 'deploy').mkdir(exist_ok=True)
        for name in DEPLOY_FILES:
            shutil.copyfile(source / name, target / name)
        deployment['configuration_sha256'] = {name: checksum((target / name).read_bytes()) for name in DEPLOY_FILES}
    (target / 'deployment.json').write_text(json.dumps(deployment, indent=2) + '\n')
    shutil.copyfile(source / 'versions.lock.yaml', stage / 'versions.lock.yaml')
    for name in ('caddy_data', 'caddy_config'):
        (stage / name).mkdir()
    metadata = {
        'schema_version': 2,
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'integration_commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
        'versions_lock_sha256': checksum((stage / 'versions.lock.yaml').read_bytes()),
        'deployment': deployment,
    }
    if bundled:
        metadata['database_dump_sha256'] = checksum((stage / 'database.dump').read_bytes())
    (stage / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')


def read_archive(archive, expected_lock, expected_mode=None):
    """Validate every member and ownership claim before extraction or Docker writes."""
    tar = tarfile.open(archive, 'r:gz')
    try:
        paths = {}
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive member; links, devices, and path traversal are refused')
            key = str(path)
            if key in paths:
                raise ValueError('Duplicate archive path refused')
            paths[key] = member

        for name, member in paths.items():
            if name == '.' and not member.isdir():
                raise ValueError('Archive root must be a directory')
            for parent in PurePosixPath(name).parents:
                if str(parent) in paths and not paths[str(parent)].isdir():
                    raise ValueError('Archive file cannot also be a parent directory')

        def content(name):
            member = paths.get(name)
            if member is None or not member.isfile():
                raise ValueError('Backup lacks required file: ' + name)
            return tar.extractfile(member).read()

        metadata = json.loads(content('metadata.json'))
        version = metadata.get('schema_version')
        if version not in (1, 2):
            raise ValueError('Unsupported backup schema')
        deployment = load_deployment() if version == 1 else validate_deployment(metadata.get('deployment'))
        mode = deployment['identity']['mode']
        if expected_mode is not None and mode != expected_mode:
            raise ValueError('Backup mode differs from --expected-mode')
        lock = content('versions.lock.yaml')
        if lock != expected_lock.read_bytes() or checksum(lock) != metadata.get('versions_lock_sha256'):
            raise ValueError('Backup does not match the explicitly supplied release lock')
        if mode == 'bundled':
            if checksum(content('database.dump')) != metadata.get('database_dump_sha256'):
                raise ValueError('Database dump checksum differs')
        elif 'database.dump' in paths or 'database_dump_sha256' in metadata:
            raise ValueError('External backup cannot contain an identity database dump')
        content('runtime/compose.env')
        if version == 1:
            for name in ('deploy/compose.yaml', 'deploy/Caddyfile'):
                content(name)
            if 'runtime/deployment.json' in paths:
                raise ValueError('Legacy backup cannot override bundled mode with a deployment descriptor')
        else:
            if json.loads(content('runtime/deployment.json')) != deployment:
                raise ValueError('Archive deployment descriptor differs from metadata')
            if set(deployment['configuration_sha256']) != set(DEPLOY_FILES):
                raise ValueError('Deployment descriptor must bind all effective deploy files')
            for name, digest in deployment['configuration_sha256'].items():
                if checksum(content('runtime/' + name)) != digest:
                    raise ValueError('Deployment configuration checksum differs: ' + name)
            validate_effective_config(deployment, {name: content('runtime/' + name) for name in DEPLOY_FILES})
            if mode == 'external':
                local_worker = deployment['directory_sync']['owner'] == 'local'
                # Credential names are derived from the archived worker config below.
                secrets = set(APP_SECRETS)
                if local_worker and 'runtime/sync/sync.json' in paths:
                    secrets.update(worker_secrets(json.loads(content('runtime/sync/sync.json'))))
                for name in paths:
                    path = PurePosixPath(name)
                    if path.parts and path.parts[0] == 'runtime':
                        if not external_path_allowed(str(PurePosixPath(*path.parts[1:])), local_worker, secrets):
                            raise ValueError('External backup contains unowned runtime state: ' + name)
                    elif name not in ('.', 'metadata.json', 'versions.lock.yaml') and (not path.parts or path.parts[0] not in ('caddy_data', 'caddy_config')):
                        raise ValueError('External backup contains unowned state: ' + name)
        return tar, metadata
    except BaseException:
        tar.close()
        raise


def extract_archive(archive, expected_lock, destination, project, expected_mode):
    tar, metadata = read_archive(archive, expected_lock, expected_mode)
    with tar:
        destination.mkdir(mode=0o700)
        for member in sorted(tar.getmembers(), key=lambda item: len(PurePosixPath(item.name).parts)):
            target = destination / PurePosixPath(member.name)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open('xb') as output:
                    shutil.copyfileobj(tar.extractfile(member), output)
                target.chmod(member.mode & 0o700)
    env = destination / 'runtime/compose.env'
    replacements = {'RUNTIME_DIR': str(destination / 'runtime'), 'RUN_UID': str(os.getuid()), 'RUN_GID': str(os.getgid()), 'COMPOSE_PROJECT_NAME': project}
    lines, seen = [], set()
    for line in env.read_text().splitlines():
        key, separator, value = line.partition('=')
        seen.add(key)
        lines.append(key + '=' + replacements[key] if separator and key in replacements else line)
    lines.extend(key + '=' + value for key, value in replacements.items() if key not in seen)
    env.write_text('\n'.join(lines) + '\n')
    env.chmod(0o600)
    return metadata


def main():
    action, *args = sys.argv[1:]
    if action == 'inspect':
        deployment, _, compose = inspect_runtime(*map(Path, args))
        print(deployment['identity']['mode'])
        print(compose)
        print(' '.join(deployment['services']))
    elif action == 'stage':
        stage_backup(*map(Path, args))
    elif action in ('verify', 'extract'):
        archive, lock, destination = map(Path, args[:3])
        project = args[3]
        expected_mode = args[4] or None
        if action == 'extract':
            metadata = extract_archive(archive, lock, destination, project, expected_mode)
        else:
            tar, metadata = read_archive(archive, lock, expected_mode)
            tar.close()
        version = metadata['schema_version']
        mode = 'bundled' if version == 1 else metadata['deployment']['identity']['mode']
        print(mode)
        print(destination / ('deploy/compose.yaml' if version == 1 else 'runtime/deploy/compose.yaml'))
    else:
        raise ValueError('Unknown recovery operation')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, tarfile.TarError, yaml.YAMLError) as error:
        raise SystemExit(str(error))
