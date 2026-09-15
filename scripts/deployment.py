#!/usr/bin/env python
"""Shared identity ownership and service selection for deployment tools."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ARTIFACT_SERVICES = {
    'headscale': 'headscale',
    'headplane': 'headplane',
    'reverse_proxy': 'proxy',
    'casdoor': 'casdoor',
    'database': 'db',
    'sync': 'worker',
}
PRODUCTS = ('headscale', 'headplane', 'casdoor')
DEPLOY_FILES = ('deploy/compose.yaml', 'deploy/compose.offline.yaml', 'deploy/Caddyfile')


def validate_issuer(issuer):
    if not isinstance(issuer, str) or not issuer or re.search(r'[\s\\\x00-\x1f\x7f]', issuer):
        raise ValueError('Identity issuer must be an HTTPS URL')
    parsed = urlsplit(issuer)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError('Invalid issuer port') from error
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or '?' in issuer or '#' in issuer
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('Identity issuer must be HTTPS without credentials, query or fragment')
    return issuer


def make_deployment(mode='bundled', issuer=None, directory_owner=None):
    if mode not in ('bundled', 'external'):
        raise ValueError('identity.mode must be bundled or external')
    owner = directory_owner if directory_owner is not None else ('external' if mode == 'external' else 'local')
    if owner not in ('local', 'external', 'disabled'):
        raise ValueError('directory_sync.owner must be local, external or disabled')
    if issuer is not None or mode == 'external':
        validate_issuer(issuer)
    artifacts = ['headscale', 'headplane', 'reverse_proxy']
    if mode == 'bundled':
        artifacts += ['casdoor', 'database']
    if owner == 'local':
        artifacts += ['sync']
    return {
        'schema_version': 1,
        'identity': {'mode': mode, 'issuer': issuer},
        'directory_sync': {'owner': owner},
        'services': [ARTIFACT_SERVICES[name] for name in artifacts],
        'artifacts': artifacts,
        'configuration_sha256': {},
    }


def deployment_from_site(site):
    identity = site.get('identity', {})
    directory = site.get('directory_sync', {})
    if not isinstance(identity, dict) or not isinstance(directory, dict):
        raise ValueError('identity and directory_sync must be objects')
    mode = identity.get('mode', 'bundled')
    issuer = identity.get('issuer')
    if mode == 'bundled':
        host = site.get('casdoor_host')
        if not isinstance(host, str) or not host:
            raise ValueError('casdoor_host is required in bundled mode')
        expected = 'https://' + host
        if issuer is not None and issuer != expected:
            raise ValueError('Bundled issuer conflicts with casdoor_host')
        issuer = expected
    return make_deployment(mode, issuer, directory.get('owner'))


def validate_deployment(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Unsupported deployment descriptor schema')
    identity, directory = value.get('identity'), value.get('directory_sync')
    if not isinstance(identity, dict) or not isinstance(directory, dict):
        raise ValueError('Deployment identity and directory_sync are required')
    if 'mode' not in identity or 'issuer' not in identity or 'owner' not in directory:
        raise ValueError('Deployment mode, issuer and directory owner are required')
    expected = make_deployment(identity['mode'], identity['issuer'], directory['owner'])
    for field in ('identity', 'directory_sync', 'services', 'artifacts'):
        if value.get(field) != expected[field]:
            raise ValueError('Deployment descriptor selection differs: ' + field)
    checksums = value.get('configuration_sha256')
    if not isinstance(checksums, dict):
        raise ValueError('Deployment configuration_sha256 must be a mapping')
    for name, digest in checksums.items():
        if not isinstance(name, str):
            raise ValueError('Invalid deployment configuration path')
        path = Path(name)
        if (not name or path.is_absolute() or '..' in path.parts
                or not re.fullmatch(r'[0-9a-f]{64}', str(digest))):
            raise ValueError('Invalid deployment configuration checksum')
    return value


def load_deployment(path=None):
    """No path means legacy bundled selection; an explicit path must exist."""
    if path is None:
        return make_deployment()
    if Path(path).is_symlink():
        raise ValueError('Deployment descriptor cannot be a symlink')
    return validate_deployment(json.loads(Path(path).read_text()))


def deployment_for_config(config_path, descriptor_path=None):
    """Find runtime metadata beside an API config or its containing sync directory."""
    config = Path(config_path).resolve()
    found = []
    for directory in (config.parent, config.parent.parent):
        descriptor = directory / 'deployment.json'
        if descriptor.exists() or descriptor.is_symlink():
            found.append(load_deployment(descriptor))
        elif (directory / 'deploy' / 'compose.yaml').exists():
            raise ValueError('Rendered runtime requires deployment.json')
    selected = load_deployment(descriptor_path) if descriptor_path is not None else None
    if found:
        if any(item != found[0] for item in found[1:]) or (selected is not None and selected != found[0]):
            raise ValueError('Explicit deployment differs from the configured runtime')
        return found[0]
    return selected if selected is not None else load_deployment()


def selected_products(deployment):
    validate_deployment(deployment)
    return tuple(name for name in PRODUCTS if name in deployment['artifacts'])


def deployment_digest(deployment):
    validate_deployment(deployment)
    encoded = json.dumps(deployment, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_configuration(runtime, deployment):
    """Reject missing or modified files recorded in the runtime descriptor."""
    validate_deployment(deployment)
    root = Path(runtime).resolve()
    if set(deployment['configuration_sha256']) != set(DEPLOY_FILES):
        raise ValueError('Deployment descriptor must bind all effective deploy files')
    for relative, expected in deployment['configuration_sha256'].items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Configuration path escapes runtime: ' + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Deployment configuration changed: ' + relative)
    # Checksums bind bytes; also verify those bytes select the declared services.
    # Keep YAML optional for legacy archive readers that never validate a runtime.
    import yaml
    expected_services = set(deployment['services'])
    for relative in DEPLOY_FILES[:2]:
        effective = yaml.safe_load((root / relative).read_text())
        if not isinstance(effective, dict) or not isinstance(effective.get('services'), dict):
            raise ValueError('Invalid effective deployment services: ' + relative)
        if set(effective['services']) != expected_services:
            raise ValueError('Effective Compose services differ from deployment: ' + relative)
        if set(effective) - {'name', 'services', 'volumes', 'secrets'}:
            raise ValueError('Unsupported deployment resource indirection: ' + relative)
        if deployment['identity']['mode'] == 'external':
            if effective.get('secrets') or set(effective.get('volumes', {})) - {'caddy_data', 'caddy_config'}:
                raise ValueError('External deployment includes identity resources')
