#!/usr/bin/env python3
"""Transfer the six deployment images without a registry (Python 3; export needs PyYAML)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PRODUCTS = ('headscale', 'headplane', 'casdoor')
ENV = dict(headscale='HEADSCALE_IMAGE', headplane='HEADPLANE_IMAGE',
           casdoor='CASDOOR_IMAGE', sync='WORKER_IMAGE', database='POSTGRES_IMAGE',
           reverse_proxy='CADDY_IMAGE')
REVISION = 'org.opencontainers.image.revision'
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(*args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=pairs)


def inputs():
    data = read_json(ROOT / 'image-inputs.json')
    require(isinstance(data, dict) and data.get('schema_version') == 1,
            'Unsupported image-inputs.json schema')
    require(data.get('platform') in ('linux/amd64', 'linux/arm64'), 'Invalid input platform')
    images = data.get('images')
    require(isinstance(images, dict) and set(images) == {'sync', 'database', 'reverse_proxy'},
            'image-inputs.json must contain sync, database, reverse_proxy')
    for reference in images.values():
        validate_reference(reference)
    return data


def validate_reference(reference):
    require(isinstance(reference, str) and bool(reference) and
            not reference.startswith('-') and not re.search(r'\s', reference),
            'Invalid image reference')


def binding():
    return dict(integration_commit=command('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
                versions_lock_sha256=digest(ROOT / 'versions.lock.yaml'),
                image_inputs_sha256=digest(ROOT / 'image-inputs.json'))


def alias(key, image_id):
    return 'offline/feishu-' + key.replace('_', '-') + ':sha256-' + image_id[7:]


def inspect(reference, engine='docker'):
    result = json.loads(command(engine, 'image', 'inspect', reference))
    require(isinstance(result, list) and len(result) == 1, 'Expected one image: ' + reference)
    image = result[0]
    require(isinstance(image, dict), 'Invalid Docker image inspection')
    image_id = image.get('Id', '')
    if engine == 'podman' and isinstance(image_id, str) and SHA.fullmatch(image_id):
        image_id = 'sha256:' + image_id
    require(isinstance(image_id, str) and image_id.startswith('sha256:') and
            SHA.fullmatch(image_id[7:]), 'Invalid image ID: ' + reference)
    labels = (image.get('Config') or {}).get('Labels') or {}
    require(isinstance(labels, dict), 'Invalid image labels: ' + reference)
    return dict(id=image_id, os=image.get('Os'), architecture=image.get('Architecture'),
                revision=labels.get(REVISION))


def validate_image(actual, expected, platform, reference):
    require(actual['os'] == 'linux' and actual['architecture'] == platform.split('/')[1],
            'Image platform mismatch: ' + reference)
    require(actual['revision'] == expected['revision'], 'Image revision mismatch: ' + reference)
    if 'id' in expected:
        require(actual['id'] == expected['id'], 'Image ID mismatch: ' + reference)


def export_bundle(args):
    engine = getattr(args, 'engine', 'docker')
    require(engine in ('docker', 'podman'), 'Export engine must be docker or podman')
    output = args.output.resolve()
    require(not output.exists() and not output.is_symlink(), 'Output already exists: ' + str(output))
    require(not command('git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=all'),
            'Export requires a clean integration Git checkout')
    config = inputs()
    require(args.platform == config['platform'], 'Platform must match image-inputs.json')
    try:
        import yaml
    except ImportError as error:
        raise ValueError('Export requires PyYAML; install requirements-build.txt') from error
    try:
        lock = yaml.safe_load((ROOT / 'versions.lock.yaml').read_text())
    except yaml.YAMLError as error:
        raise ValueError('Invalid versions.lock.yaml: ' + str(error)) from error
    require(isinstance(lock, dict) and lock.get('schema_version') == 1 and
            isinstance(lock.get('components'), dict), 'Invalid versions.lock.yaml')
    manifest = dict(schema_version=1, exporter=engine, platform=args.platform, **binding(), images={})
    references = dict(config['images'])
    revisions = dict(sync=manifest['integration_commit'], database=None, reverse_proxy=None)
    for key in PRODUCTS:
        component = lock['components'].get(key)
        require(isinstance(component, dict), 'Missing product lock: ' + key)
        references[key] = component.get('image')
        validate_reference(references[key])
        revision = component.get('source_commit')
        require(isinstance(revision, str) and COMMIT.fullmatch(revision), 'Invalid source commit: ' + key)
        revisions[key] = revision
    if args.pull_supporting_images:
        for key in ('database', 'reverse_proxy'):
            reference = references[key]
            if engine == 'podman':
                first = reference.split('/')[0]
                if '/' not in reference or not ('.' in first or ':' in first or first == 'localhost'):
                    reference = 'docker.io/' + (reference if '/' in reference else 'library/' + reference)
            command(engine, 'pull', '--platform', args.platform, reference)
    for key in ENV:
        actual = inspect(references[key], engine)
        # Supporting images may carry their own upstream revision label.
        if key not in PRODUCTS and key != 'sync':
            revisions[key] = actual['revision']
        validate_image(actual, {'revision': revisions[key]}, args.platform, references[key])
        manifest['images'][key] = dict(actual, reference=references[key],
                                      source_commit=revisions[key] if key in PRODUCTS or key == 'sync' else None,
                                      alias=alias(key, actual['id']))
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.image-bundle-', dir=output.parent))
    try:
        archive = staging / 'images.tar'
        save_flags = ['--format', 'docker-archive', '--multi-image-archive'] if engine == 'podman' else []
        command(engine, 'image', 'save', *save_flags, '--output', str(archive),
                *(image['id'] for image in manifest['images'].values()))
        manifest['archive'] = dict(file='images.tar', sha256=digest(archive))
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        current_binding = binding()
        require(current_binding == {key: manifest[key] for key in current_binding}, 'Source changed during export')
        require(not output.exists() and not output.is_symlink(), 'Output appeared during export')
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print('Exported six images to ' + str(output))


def validate_bundle(bundle):
    manifest = read_json(bundle / 'manifest.json')
    require(isinstance(manifest, dict) and manifest.get('schema_version') == 1,
            'Unsupported bundle manifest schema')
    for key, value in binding().items():
        require(manifest.get(key) == value, 'Bundle source binding mismatch: ' + key)
    config = inputs()
    require(manifest.get('platform') == config['platform'], 'Bundle platform differs from image-inputs.json')
    images = manifest.get('images')
    require(isinstance(images, dict) and set(images) == set(ENV), 'Bundle must contain all six images')
    for key, item in images.items():
        require(isinstance(item, dict), 'Invalid image record: ' + key)
        image_id = item.get('id')
        require(isinstance(image_id, str) and image_id.startswith('sha256:') and
                SHA.fullmatch(image_id[7:]), 'Invalid manifest image ID: ' + key)
        require(item.get('alias') == alias(key, image_id), 'Invalid offline alias: ' + key)
        require(item.get('os') == 'linux' and
                item.get('architecture') == manifest['platform'].split('/')[1], 'Invalid image platform: ' + key)
        validate_reference(item.get('reference'))
        require(item.get('revision') is None or isinstance(item['revision'], str), 'Invalid revision: ' + key)
        if key in PRODUCTS or key == 'sync':
            source = item.get('source_commit')
            require(isinstance(source, str) and COMMIT.fullmatch(source) and source == item.get('revision'),
                    'Invalid source revision: ' + key)
            if key == 'sync':
                require(source == manifest['integration_commit'], 'Sync revision differs from integration commit')
        if key in config['images']:
            require(item['reference'] == config['images'][key], 'Image reference differs from inputs: ' + key)
    archive = manifest.get('archive')
    require(isinstance(archive, dict) and archive.get('file') == 'images.tar' and
            isinstance(archive.get('sha256'), str) and SHA.fullmatch(archive['sha256']), 'Invalid archive metadata')
    require(digest(bundle / 'images.tar') == archive['sha256'], 'Bundle archive checksum mismatch')
    return manifest


def validate_daemon(platform):
    info = json.loads(command('docker', 'info', '--format', '{{json .}}'))
    require(isinstance(info, dict), 'Invalid Docker daemon information')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(info.get('Architecture'), info.get('Architecture'))
    require(info.get('OSType') == 'linux' and 'linux/' + str(arch) == platform,
            'Docker daemon must run Linux with bundle architecture ' + platform)


def validate_loaded(manifest, aliases=False):
    for item in manifest['images'].values():
        reference = item['alias'] if aliases else item['id']
        validate_image(inspect(reference), item, manifest['platform'], reference)


def env_values(manifest):
    return {ENV[key]: image['alias'] for key, image in manifest['images'].items()}


def pin_env(path, values):
    original = path.read_bytes()
    lines = original.decode().splitlines(keepends=True)
    seen = set()
    for index, line in enumerate(lines):
        match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=', line)
        if match and match[1] in values:
            key = match[1]
            require(key not in seen, 'Duplicate image variable in compose.env: ' + key)
            seen.add(key)
            lines[index] = key + '=' + values[key] + ('\r\n' if line.endswith('\r\n') else '\n')
    missing = set(values) - seen
    if missing and lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines.extend(key + '=' + values[key] + '\n' for key in values if key in missing)
    content = ''.join(lines).encode()
    if content == original:
        return
    backup_fd, backup_name = tempfile.mkstemp(prefix='compose.env.backup-', dir=path.parent)
    with os.fdopen(backup_fd, 'wb') as stream:
        stream.write(original)
    fd, temporary = tempfile.mkstemp(prefix='.compose.env-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
        require(path.read_bytes() == original, 'compose.env changed during import; retry')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print('Saved previous environment to ' + backup_name)


def use_bundle(args):
    manifest = validate_bundle(args.bundle.resolve())
    env_path = args.runtime.resolve() / 'compose.env'
    require(env_path.is_file() and not env_path.is_symlink(), 'Configure runtime/compose.env before import/check')
    validate_daemon(manifest['platform'])
    if args.action == 'import':
        command('docker', 'image', 'load', '--input', str(args.bundle.resolve() / 'images.tar'))
        validate_loaded(manifest)
        for item in manifest['images'].values():
            command('docker', 'image', 'tag', item['id'], item['alias'])
        validate_loaded(manifest, aliases=True)
        pin_env(env_path, env_values(manifest))
        print('Imported and pinned six images. Services have not been started.')
    else:
        validate_loaded(manifest, aliases=True)
        values = env_values(manifest)
        found = {}
        for line in env_path.read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)', line)
            if match and match[1] in values:
                require(match[1] not in found, 'Duplicate image variable: ' + match[1])
                found[match[1]] = match[2]
        require(found == values, 'compose.env image pins do not match this bundle; import it again')
        print('Bundle, Docker images, and compose.env pins verified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    export = actions.add_parser('export')
    export.add_argument('--engine', choices=('docker', 'podman'), default='docker')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--platform', choices=('linux/amd64', 'linux/arm64'), default='linux/amd64')
    export.add_argument('--pull-supporting-images', action='store_true')
    for action in ('import', 'check'):
        sub = actions.add_parser(action)
        sub.add_argument('--bundle', type=Path, required=True)
        sub.add_argument('--runtime', type=Path, default=Path('.runtime'))
    args = parser.parse_args()
    try:
        (export_bundle if args.action == 'export' else use_bundle)(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as error:
        parser.exit(1, 'image-bundle: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
