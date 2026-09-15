#!/usr/bin/env python
"""Render private deployment configuration. Existing credentials are never rotated."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit
import yaml

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / 'scripts'))
from deployment import deployment_from_site, load_deployment, selected_products, verify_configuration

HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
NAME = re.compile(r"[A-Za-z0-9_-]{1,80}$")

def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)
    path.chmod(0o600)


def check_existing_identity(output, deployment, site):
    """Changing an existing issuer or ownership mode requires a migration."""
    descriptor = output / 'deployment.json'
    if descriptor.exists():
        previous = load_deployment(descriptor)
        if previous['identity'] != deployment['identity']:
            raise ValueError('Existing runtime identity mode or issuer differs; migration required')
        if previous['directory_sync'] != deployment['directory_sync']:
            raise ValueError('Existing directory synchronization ownership differs; migration required')
        # Validate old generated files before replacing them with updated templates.
        expected_files = {'deploy/compose.yaml', 'deploy/compose.offline.yaml', 'deploy/Caddyfile'}
        if not expected_files.issubset(previous['configuration_sha256']):
            raise ValueError('Existing deployment descriptor omits generated configuration checksums')
        verify_configuration(output, previous)
    else:
        # Old runtimes have no descriptor and always own their Casdoor instance.
        markers = ('compose.env', 'casdoor', 'headscale/config.yaml', 'headplane/config.yaml',
                   'secrets/db_password')
        if any((output / name).exists() for name in markers):
            if deployment['identity']['mode'] != 'bundled':
                raise ValueError('Legacy runtime is bundled; migration required')
            if deployment['directory_sync']['owner'] != 'local':
                raise ValueError('Legacy directory synchronization ownership is local; migration required')
    if deployment['identity']['mode'] == 'external':
        for name in ('casdoor', 'secrets/db_password'):
            path = output / name
            if path.exists() or path.is_symlink():
                raise ValueError('External runtime contains bundled identity state; migration required')
    issuers = []
    for name in ('headscale', 'headplane'):
        path = output / name / 'config.yaml'
        if path.exists():
            config = yaml.safe_load(path.read_text())
            if not isinstance(config, dict) or not isinstance(config.get('oidc'), dict):
                raise ValueError('Existing runtime has invalid OIDC configuration')
            if (deployment['identity']['mode'] == 'external'
                    and config['oidc'].get('client_id') != site[name + '_client_id']):
                raise ValueError('Existing OIDC client ID differs; credential migration required')
            issuers.append(config['oidc'].get('issuer'))
    env_path = output / 'compose.env'
    if not descriptor.exists() and env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith('CASDOOR_HOST='):
                issuers.append('https://' + line.split('=', 1)[1])
    app_path = output / 'casdoor/app.conf'
    if not descriptor.exists() and app_path.exists():
        for line in app_path.read_text().splitlines():
            key, separator, value = line.partition('=')
            if separator and key.strip() in ('origin', 'originFrontend'):
                issuers.append(value.strip())
    if any(issuer != deployment['identity']['issuer'] for issuer in issuers):
        raise ValueError('Existing runtime issuer differs; migration required')


def client_secret(output, name, supplied, external):
    """Read credentials before any writes; rerendering never rotates them."""
    path = output / 'secrets' / name
    existing = path.read_text().strip() if path.exists() else None
    value = Path(supplied).read_text().strip() if supplied is not None else None
    for candidate in (existing, value):
        if candidate is not None and (not candidate or any(c.isspace() for c in candidate)):
            raise ValueError(f'{name} must be nonempty and contain no whitespace')
    if existing is not None:
        if value is not None and value != existing:
            raise ValueError(f'{name} differs from the existing credential; rotation is separate')
        return existing
    if value is not None:
        return value
    if external:
        raise ValueError(f'External mode requires an existing {name} credential file')
    return secrets.token_hex(32)


def deployment_files(deployment):
    """Filter the maintained bundled templates, including unused named resources."""
    compose = yaml.safe_load((SOURCE_ROOT / 'deploy/compose.yaml').read_text())
    compose['services'] = {name: service for name, service in compose['services'].items()
                           if name in deployment['services']}
    offline = yaml.safe_load((SOURCE_ROOT / 'deploy/compose.offline.yaml').read_text())
    offline['services'] = {name: service for name, service in offline['services'].items()
                           if name in deployment['services']}
    caddy = (SOURCE_ROOT / 'deploy/Caddyfile').read_text()
    if deployment['identity']['mode'] == 'external':
        compose.pop('secrets', None)
        compose['volumes'].pop('casdoor_db', None)
        compose['services']['proxy']['environment'].pop('CASDOOR_HOST', None)
        # Remove only the known local identity route; fail if its template has drifted.
        caddy = caddy.replace((SOURCE_ROOT / 'deploy/Caddyfile.identity').read_text(), '')
        if 'casdoor' in caddy or 'CASDOOR_HOST' in caddy:
            raise ValueError('External proxy configuration contains a bundled identity route')
    return {
        'deploy/compose.yaml': yaml.safe_dump(compose, sort_keys=False),
        'deploy/compose.offline.yaml': yaml.safe_dump(offline, sort_keys=False),
        'deploy/Caddyfile': caddy,
    }


def render(site, output, *, headscale_client_secret_file=None, headplane_client_secret_file=None):
    if not isinstance(site, dict):
        raise ValueError('Site settings must be an object')
    deployment = deployment_from_site(site)
    external = deployment['identity']['mode'] == 'external'
    hosts = ['headscale_host', 'headplane_host', 'tailnet_domain']
    if not external:
        hosts.append('casdoor_host')
    for field in hosts:
        if not isinstance(site.get(field), str) or not HOST.fullmatch(site[field]):
            raise ValueError(f'{field} must be a DNS hostname without scheme or path')
    for field in ('organization', 'headscale_client_id', 'headplane_client_id', 'admission_group'):
        if not isinstance(site.get(field), str) or not NAME.fullmatch(site[field]):
            raise ValueError(f'{field} must be a simple identifier')
    if external and site['headscale_client_id'] == site['headplane_client_id']:
        raise ValueError('External mode requires separate Headscale and Headplane client IDs')
    if external and urlsplit(deployment['identity']['issuer']).hostname in {site[x] for x in hosts}:
        raise ValueError('External issuer hostname must be distinct from Tailnet hostnames')
    if len({site[x] for x in hosts}) != len(hosts):
        raise ValueError('Service hostnames and tailnet DNS suffix must be distinct')
    inputs = json.loads((SOURCE_ROOT / 'image-inputs.json').read_text())
    if not isinstance(inputs, dict) or inputs.get('schema_version') != 1:
        raise ValueError('Unsupported image input schema')
    support_images = inputs.get('images')
    if not isinstance(support_images, dict):
        raise ValueError('Image inputs must contain an images mapping')
    for component in ('reverse_proxy', 'database', 'sync'):
        if component not in deployment['artifacts']:
            continue
        image = support_images.get(component)
        if not isinstance(image, str) or not image or image.startswith('-') or re.search(r'\s', image):
            raise ValueError(f'Invalid supporting image: {component}')
    try:
        lock = yaml.safe_load((SOURCE_ROOT / 'versions.lock.yaml').read_text())
    except yaml.YAMLError as error:
        raise ValueError('Invalid source lock YAML') from error
    if not isinstance(lock, dict) or lock.get('schema_version') != 1:
        raise ValueError('Unsupported source lock schema')
    components = lock.get('components')
    if not isinstance(components, dict):
        raise ValueError('Source lock components must be a mapping')
    product_images = {}
    for component in selected_products(deployment):
        entry = components.get(component)
        image = entry.get('image') if isinstance(entry, dict) else None
        if not isinstance(image, str) or not image or image.startswith('-') or re.search(r'\s', image):
            raise ValueError(f'Invalid source lock image: {component}')
        product_images[component] = image
    output = Path(output).resolve()
    if any(c in str(output) for c in '\n\r$# '):
        raise ValueError('Runtime directory must not contain whitespace, dollar signs, or #')
    check_existing_identity(output, deployment, site)
    credentials = {
        'headscale_oidc_secret': client_secret(output, 'headscale_oidc_secret', headscale_client_secret_file, external),
        'headplane_oidc_secret': client_secret(output, 'headplane_oidc_secret', headplane_client_secret_file, external),
    }
    if external and len(set(credentials.values())) != 2:
        raise ValueError('External mode requires separate Headscale and Headplane client credentials')
    generated = deployment_files(deployment)
    db_password = None
    if not external:
        path = output / 'secrets/db_password'
        db_password = path.read_text().strip() if path.exists() else secrets.token_hex(32)
        if not re.fullmatch(r'[a-f0-9]{64}', db_password):
            raise ValueError('Generated database password must remain a 64-character hex value')
        credentials['db_password'] = db_password
    cookie = output / 'secrets/cookie_secret'
    if not cookie.exists():
        credentials['cookie_secret'] = secrets.token_hex(16)
    # All settings, imported credentials and templates are validated before writing.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    directories = ['headscale/data', 'headplane/data', 'secrets']
    if not external:
        directories += ['casdoor/logs', 'casdoor/files']
    if deployment['directory_sync']['owner'] == 'local':
        directories += ['sync/state']
    for directory in directories:
        (output / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in credentials.items():
        path = output / 'secrets' / name
        if not path.exists():
            write_private(path, value)
    # Keep API key absent until generated by the running Headscale instance.
    issuer = deployment['identity']['issuer']
    if not external:
        write_private(output / 'casdoor/app.conf', f'''appname = casdoor
httpport = 8000
runmode = prod
copyrequestbody = true
driverName = postgres
dataSourceName = "user=casdoor password={db_password} host=db port=5432 sslmode=disable dbname=casdoor"
dbName = casdoor
showSql = false
origin = {issuer}
originFrontend = {issuer}
isDemoMode = false
initDataNewOnly = true
logPostOnly = true
frontendBaseDir = "./web/build"
''')
    hs = {
        'server_url': 'https://' + site['headscale_host'],
        'listen_addr': '0.0.0.0:8080', 'metrics_listen_addr': '0.0.0.0:9090',
        'noise': {'private_key_path': '/var/lib/headscale/noise_private.key'},
        'prefixes': {'v4': '100.64.0.0/10', 'v6': 'fd7a:115c:a1e0::/48', 'allocation': 'sequential'},
        'database': {'type': 'sqlite', 'sqlite': {'path': '/var/lib/headscale/db.sqlite', 'write_ahead_log': True}},
        'node': {'expiry': '30d'},
        'dns': {'magic_dns': True, 'base_domain': site['tailnet_domain'], 'nameservers': {'global': ['1.1.1.1', '8.8.8.8']}},
        'derp': {'server': {'enabled': False}, 'urls': ['https://controlplane.tailscale.com/derpmap/default'], 'auto_update_enabled': True, 'update_frequency': '3h'},
        'policy': {'mode': 'file', 'path': '/etc/headscale/policy.json'},
        'unix_socket': '/var/lib/headscale/headscale.sock', 'unix_socket_permission': '0770',
        'oidc': {'issuer': issuer, 'client_id': site['headscale_client_id'], 'client_secret_path': '/run/secrets/oidc_secret',
                 'only_start_if_oidc_is_available': True, 'scope': ['openid', 'profile', 'email', 'groups'],
                 'allowed_groups': [site['organization'] + '/' + site['admission_group']],
                 'email_verified_required': True, 'use_expiry_from_token': False, 'pkce': {'enabled': True, 'method': 'S256'}}
    }
    hp = {
        'server': {'host': '0.0.0.0', 'port': 3000, 'base_url': 'https://' + site['headplane_host'],
                   'cookie_secret_path': '/run/secrets/cookie_secret', 'cookie_secure': True, 'cookie_max_age': 300},
        'headscale': {'url': 'http://headscale:8080', 'public_url': 'https://' + site['headscale_host'], 'api_key_path': '/run/secrets/headscale_api_key'},
        'oidc': {'issuer': issuer, 'client_id': site['headplane_client_id'], 'client_secret_path': '/run/secrets/oidc_secret',
                 'use_pkce': True, 'disable_api_key_login': True, 'scope': 'openid email profile groups',
                 'default_role': 'member', 'role_claim': 'headplane_role', 'allow_weak_rsa_keys': False}
    }
    # JSON is a strict subset of YAML; retain YAML filenames expected by both apps.
    for filename, config in (('headscale/config.yaml', hs), ('headplane/config.yaml', hp)):
        write_private(output / filename, json.dumps(config, indent=2) + '\n')
    policy = output / 'headscale/policy.json'
    if not policy.exists():
        write_private(policy, json.dumps({'groups': {}, 'acls': []}, indent=2) + '\n')
    env = {
        'COMPOSE_PROJECT_NAME': 'integrated-tailnet', 'RUNTIME_DIR': str(output), 'RUN_UID': str(os.getuid()), 'RUN_GID': str(os.getgid()),
        'HEADSCALE_HOST': site['headscale_host'], 'HEADPLANE_HOST': site['headplane_host'],
        'HEADSCALE_IMAGE': product_images['headscale'], 'HEADPLANE_IMAGE': product_images['headplane'],
        'CADDY_IMAGE': inputs['images']['reverse_proxy'],
        'HEADPLANE_ORGANIZATION_NAME': '', 'HEADPLANE_ORGANIZATION_LOGO_URL': '',
        'HEADPLANE_ORGANIZATION_NAME_EN': '', 'HEADPLANE_ORGANIZATION_NAME_ZH': ''
    }
    if not external:
        env.update(CASDOOR_HOST=site['casdoor_host'], CASDOOR_IMAGE=product_images['casdoor'],
                   POSTGRES_IMAGE=inputs['images']['database'])
    if deployment['directory_sync']['owner'] == 'local':
        env['WORKER_IMAGE'] = inputs['images']['sync']
    # Preserve operator image digest pins on rerender.
    env_path = output / 'compose.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key in env and (key.endswith('_IMAGE') or key == 'COMPOSE_PROJECT_NAME' or key.startswith('HEADPLANE_ORGANIZATION_')):
                env[key] = value
    write_private(env_path, ''.join(f'{key}={value}\n' for key, value in env.items()))
    for name, content in generated.items():
        write_private(output / name, content)
        deployment['configuration_sha256'][name] = hashlib.sha256(content.encode()).hexdigest()
    write_private(output / 'deployment.json', json.dumps(deployment, indent=2) + '\n')
    return output

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, type=Path)
    parser.add_argument('--output', default='.runtime', type=Path)
    parser.add_argument('--headscale-client-secret-file', type=Path)
    parser.add_argument('--headplane-client-secret-file', type=Path)
    args = parser.parse_args()
    try:
        render(json.loads(args.site.read_text()), args.output,
               headscale_client_secret_file=args.headscale_client_secret_file,
               headplane_client_secret_file=args.headplane_client_secret_file)
    except (ValueError, OSError, yaml.YAMLError) as error:
        parser.exit(1, f'Configuration failed: {error}\n')
    print('Private configuration generated; existing secrets, image pins and policy preserved.')
