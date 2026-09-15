#!/usr/bin/env python
"""Transfer selected deployment images without a registry."""
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
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import (load_deployment, validate_deployment, selected_products,
                        deployment_digest, verify_configuration)

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


def inputs(deployment=None):
    data = read_json(ROOT / 'image-inputs.json')
    require(isinstance(data, dict) and data.get('schema_version') == 1,
            'Unsupported image-inputs.json schema')
    require(data.get('platform') in ('linux/amd64', 'linux/arm64'), 'Invalid input platform')
    images = data.get('images')
    required = set(deployment['artifacts']) - set(PRODUCTS) if deployment else {'sync', 'database', 'reverse_proxy'}
    require(isinstance(images, dict) and required <= set(images),
            'image-inputs.json is missing selected support images')
    if deployment is None:
        require(set(images) == required, 'image-inputs.json must contain sync, database, reverse_proxy')
    for key in required:
        validate_reference(images[key])
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
    return 'offline/tailnet-' + key.replace('_', '-') + ':sha256-' + image_id[7:]


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
    descriptor_path = getattr(args, 'deployment', None)
    deployment = load_deployment(descriptor_path)
    if descriptor_path:
        verify_configuration(Path(descriptor_path).parent, deployment)
    config = inputs(deployment if descriptor_path else None)
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
    manifest = dict(schema_version=2 if descriptor_path else 1, exporter=engine, platform=args.platform, **binding(), images={})
    if descriptor_path:
        manifest.update(deployment=deployment, deployment_sha256=deployment_digest(deployment))
    references = {key: value for key, value in config['images'].items() if key in deployment['artifacts']}
    revisions = dict(sync=manifest['integration_commit'], database=None, reverse_proxy=None)
    for key in selected_products(deployment):
        component = lock['components'].get(key)
        require(isinstance(component, dict), 'Missing product lock: ' + key)
        references[key] = component.get('image')
        validate_reference(references[key])
        revision = component.get('source_commit')
        require(isinstance(revision, str) and COMMIT.fullmatch(revision), 'Invalid source commit: ' + key)
        revisions[key] = revision
    if args.pull_supporting_images:
        for key in ('database', 'reverse_proxy'):
            if key not in references:
                continue
            reference = references[key]
            if engine == 'podman':
                first = reference.split('/')[0]
                if '/' not in reference or not ('.' in first or ':' in first or first == 'localhost'):
                    reference = 'docker.io/' + (reference if '/' in reference else 'library/' + reference)
            command(engine, 'pull', '--platform', args.platform, reference)
    for key in deployment['artifacts']:
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
        if descriptor_path:
            validate_archive_inventory(archive, manifest['images'])
        manifest['archive'] = dict(file='images.tar', sha256=digest(archive))
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        if descriptor_path:
            require(load_deployment(descriptor_path) == deployment, 'Deployment changed during export')
            verify_configuration(Path(descriptor_path).parent, deployment)
        current_binding = binding()
        require(current_binding == {key: manifest[key] for key in current_binding}, 'Source changed during export')
        require(not output.exists() and not output.is_symlink(), 'Output appeared during export')
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f'Exported {len(manifest["images"])} images to {output}')


def validate_archive_inventory(path, images):
    """Check every loadable Docker/OCI image before giving the archive to Docker."""
    expected = {item['id'] for item in images.values()}
    with tarfile.open(path, 'r:*') as archive:
        members = {}
        for member in archive:
            name = member.name.removeprefix('./')
            require(name not in members, 'Duplicate archive member: ' + name)
            require(not Path(name).is_absolute() and '..' not in Path(name).parts and
                    (member.isfile() or member.isdir()), 'Unsafe image archive member: ' + name)
            members[name] = member

        def read(name):
            member = members.get(name)
            require(member is not None and member.isfile(), 'Missing archive image metadata: ' + name)
            require(member.size <= 16 * 1024 * 1024, 'Image metadata exceeds size limit')
            return archive.extractfile(member).read()

        def document(name):
            value = json.loads(read(name))
            require(isinstance(value, dict), 'Invalid archive image metadata: ' + name)
            return value

        entries = json.loads(read('manifest.json'))
        require(isinstance(entries, list) and entries, 'Invalid Docker save image manifest')
        actual = set()
        for entry in entries:
            require(isinstance(entry, dict) and isinstance(entry.get('Config'), str),
                    'Invalid Docker save image entry')
            actual.add('sha256:' + hashlib.sha256(read(entry['Config'])).hexdigest())
        require(actual == expected, 'Archive image inventory differs from bundle manifest')

        # Modern Docker archives may also expose an OCI index. Validate both views
        # so another loader cannot select an extra image hidden from manifest.json.
        if 'index.json' in members:
            actual = set()
            def visit(descriptor, depth=0):
                require(depth < 8 and isinstance(descriptor, dict), 'Invalid OCI image descriptor')
                digest_value = descriptor.get('digest', '')
                require(isinstance(digest_value, str) and digest_value.startswith('sha256:') and
                        SHA.fullmatch(digest_value[7:]), 'Invalid OCI image digest')
                payload = read('blobs/sha256/' + digest_value[7:])
                require(hashlib.sha256(payload).hexdigest() == digest_value[7:] and
                        len(payload) == descriptor.get('size'), 'OCI image descriptor checksum differs')
                value = json.loads(payload)
                require(isinstance(value, dict), 'Invalid OCI image manifest')
                if 'manifests' in value:
                    require(isinstance(value['manifests'], list), 'Invalid OCI image index')
                    for child in value['manifests']:
                        visit(child, depth + 1)
                else:
                    config = value.get('config', {})
                    digest_value = config.get('digest', '')
                    require(isinstance(digest_value, str) and digest_value.startswith('sha256:') and
                            SHA.fullmatch(digest_value[7:]), 'Invalid OCI image config digest')
                    payload = read('blobs/sha256/' + digest_value[7:])
                    require(hashlib.sha256(payload).hexdigest() == digest_value[7:] and
                            len(payload) == config.get('size'), 'OCI image config checksum differs')
                    actual.add(digest_value)
            index = document('index.json')
            require(isinstance(index.get('manifests'), list), 'Invalid OCI archive index')
            for descriptor in index['manifests']:
                visit(descriptor)
            require(actual == expected, 'OCI archive inventory differs from bundle manifest')


def validate_bundle(bundle, deployment=None):
    manifest = read_json(bundle / 'manifest.json')
    require(isinstance(manifest, dict) and manifest.get('schema_version') in (1, 2),
            'Unsupported bundle manifest schema')
    schema = manifest['schema_version']
    selection = deployment or load_deployment()
    if schema == 2:
        require(deployment is not None, 'Schema 2 bundle requires a deployment descriptor')
        validate_deployment(manifest.get('deployment'))
        require(manifest['deployment'] == selection, 'Bundle deployment differs from runtime')
        require(manifest.get('deployment_sha256') == deployment_digest(selection),
                'Bundle deployment checksum mismatch')
    else:
        require(selection['identity']['mode'] == 'bundled' and
                selection['artifacts'] == load_deployment()['artifacts'],
                'Legacy bundle requires the complete bundled deployment')
    for key, value in binding().items():
        require(manifest.get(key) == value, 'Bundle source binding mismatch: ' + key)
    config = inputs(selection if schema == 2 else None)
    require(manifest.get('platform') == config['platform'], 'Bundle platform differs from image-inputs.json')
    images = manifest.get('images')
    require(isinstance(images, dict) and set(images) == set(selection['artifacts']),
            'Bundle images differ from deployment selection')
    # Schema 2 validates the product catalog as well as its checksum binding.
    components = {}
    if schema == 2:
        import yaml
        components = yaml.safe_load((ROOT / 'versions.lock.yaml').read_text())['components']
    for key, item in images.items():
        require(isinstance(item, dict), 'Invalid image record: ' + key)
        image_id = item.get('id')
        require(isinstance(image_id, str) and image_id.startswith('sha256:') and
                SHA.fullmatch(image_id[7:]), 'Invalid manifest image ID: ' + key)
        # Legacy aliases remain valid; source/checksum binding is still mandatory.
        aliases = (alias(key, image_id),
                   'offline/feishu-' + key.replace('_', '-') + ':sha256-' + image_id[7:])
        require(item.get('alias') in aliases, 'Invalid offline alias: ' + key)
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
        if schema == 2 and key in PRODUCTS:
            expected = components[key]
            require(item['reference'] == expected['image'] and item['source_commit'] == expected['source_commit'],
                    'Product image differs from source lock: ' + key)
        if key in config['images']:
            require(item['reference'] == config['images'][key], 'Image reference differs from inputs: ' + key)
    archive = manifest.get('archive')
    require(isinstance(archive, dict) and archive.get('file') == 'images.tar' and
            isinstance(archive.get('sha256'), str) and SHA.fullmatch(archive['sha256']), 'Invalid archive metadata')
    require(digest(bundle / 'images.tar') == archive['sha256'], 'Bundle archive checksum mismatch')
    if schema == 2:
        validate_archive_inventory(bundle / 'images.tar', images)
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
    descriptor_path = getattr(args, 'deployment', None)
    runtime_descriptor = args.runtime.resolve() / 'deployment.json'
    if runtime_descriptor.exists() or runtime_descriptor.is_symlink():
        runtime_deployment = load_deployment(runtime_descriptor)
        if descriptor_path:
            require(load_deployment(descriptor_path) == runtime_deployment,
                    'Selected deployment differs from runtime descriptor')
        descriptor_path = runtime_descriptor
    elif (args.runtime.resolve() / 'deploy').exists() or (args.runtime.resolve() / 'deploy').is_symlink():
        raise ValueError('Rendered runtime requires deployment.json before image import/check')
    deployment = load_deployment(descriptor_path) if descriptor_path else None
    if descriptor_path:
        verify_configuration(args.runtime.resolve(), deployment)
    manifest = validate_bundle(args.bundle.resolve(), deployment)
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
        print(f'Imported and pinned {len(manifest["images"])} images. Services have not been started.')
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
    export.add_argument('--deployment', type=Path)
    export.add_argument('--engine', choices=('docker', 'podman'), default='docker')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--platform', choices=('linux/amd64', 'linux/arm64'), default='linux/amd64')
    export.add_argument('--pull-supporting-images', action='store_true')
    for action in ('import', 'check'):
        sub = actions.add_parser(action)
        sub.add_argument('--deployment', type=Path)
        sub.add_argument('--bundle', type=Path, required=True)
        sub.add_argument('--runtime', type=Path, default=Path('.runtime'))
    args = parser.parse_args()
    try:
        (export_bundle if args.action == 'export' else use_bundle)(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError, tarfile.TarError) as error:
        parser.exit(1, 'image-bundle: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
