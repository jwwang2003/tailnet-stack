import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('configure', ROOT / 'scripts/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)

class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.site = json.loads((ROOT / 'deploy/site.example.json').read_text())
        self.inputs = json.loads((ROOT / 'image-inputs.json').read_text())
        self.components = yaml.safe_load((ROOT / 'versions.lock.yaml').read_text())['components']
    def test_rerender_preserves_credentials_policy_and_digest_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configure.render(self.site, root)
            secret = (root / 'secrets/cookie_secret').read_text()
            self.assertEqual(len(secret), 32)
            policy = root / 'headscale/policy.json'
            policy.write_text('{"acls": [{"action":"accept"}]}')
            env = root / 'compose.env'
            env.write_text(env.read_text().replace(self.inputs['images']['reverse_proxy'], 'caddy@sha256:' + 'a'*64))
            env.write_text(env.read_text().replace('COMPOSE_PROJECT_NAME=integrated-tailnet', 'COMPOSE_PROJECT_NAME=restored-tailnet'))
            env.write_text(env.read_text().replace('HEADPLANE_ORGANIZATION_NAME=', "HEADPLANE_ORGANIZATION_NAME='飞捷科思 · Fysics'").replace('HEADPLANE_ORGANIZATION_LOGO_URL=', 'HEADPLANE_ORGANIZATION_LOGO_URL=https://example.com/logo.svg'))
            env.write_text(env.read_text().replace('HEADPLANE_ORGANIZATION_NAME_EN=', 'HEADPLANE_ORGANIZATION_NAME_EN=Headplane Fysics').replace('HEADPLANE_ORGANIZATION_NAME_ZH=', 'HEADPLANE_ORGANIZATION_NAME_ZH=Headplane 飞捷科思'))
            configure.render(self.site, root)
            self.assertIn('HEADPLANE_ORGANIZATION_NAME_EN=Headplane Fysics', env.read_text())
            self.assertIn('HEADPLANE_ORGANIZATION_NAME_ZH=Headplane 飞捷科思', env.read_text())
            self.assertIn("HEADPLANE_ORGANIZATION_NAME='飞捷科思 · Fysics'", env.read_text())
            self.assertIn('HEADPLANE_ORGANIZATION_LOGO_URL=https://example.com/logo.svg', env.read_text())
            self.assertIn('COMPOSE_PROJECT_NAME=restored-tailnet', env.read_text())
            self.assertEqual(secret, (root / 'secrets/cookie_secret').read_text())
            self.assertIn('accept', policy.read_text())
            self.assertIn('caddy@sha256:', env.read_text())
            self.assertEqual((root / 'casdoor/app.conf').stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / 'secrets/headscale_api_key').exists())
    def test_fresh_core_and_legacy_runtime_rerender(self):
        with tempfile.TemporaryDirectory() as directory:
            root = configure.render(self.site, directory)
            env = root / 'compose.env'
            fresh = dict(line.split('=', 1) for line in env.read_text().splitlines())
            self.assertEqual(fresh['COMPOSE_PROJECT_NAME'], 'integrated-tailnet')
            for component, key in (('headscale', 'HEADSCALE_IMAGE'),
                                   ('headplane', 'HEADPLANE_IMAGE'),
                                   ('casdoor', 'CASDOOR_IMAGE')):
                self.assertEqual(fresh[key], self.components[component]['image'])
            for component, key in (('sync', 'WORKER_IMAGE'), ('database', 'POSTGRES_IMAGE'),
                                   ('reverse_proxy', 'CADDY_IMAGE')):
                self.assertEqual(fresh[key], self.inputs['images'][component])
            self.assertFalse((root / 'secrets/feishu_app_secret').exists())
            self.assertFalse((root / 'sync/sync.json').exists())
            legacy = dict(fresh, COMPOSE_PROJECT_NAME='feishu-tailnet',
                          HEADSCALE_IMAGE='feishu/headscale:2026.09-rc.1',
                          HEADPLANE_IMAGE='offline/feishu-headplane:sha256-' + 'a' * 64,
                          CASDOOR_IMAGE='feishu/casdoor@sha256:' + 'b' * 64,
                          WORKER_IMAGE='feishu/sync:2026.09-rc.1')
            env.write_text(''.join(f'{key}={value}\n' for key, value in legacy.items()))
            configure.render(self.site, root)
            self.assertEqual(dict(line.split('=', 1) for line in env.read_text().splitlines()), legacy)

    def test_worker_is_an_optional_compose_profile(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'deploy/compose.yaml').read_text())
        self.assertEqual(compose['name'], 'integrated-tailnet')
        services = compose['services']
        self.assertEqual(services['worker']['profiles'], ['sync'])
        for name in ('db', 'casdoor', 'headscale', 'headplane', 'proxy'):
            self.assertNotIn('worker', services[name].get('depends_on', {}))
        self.assertNotIn('feishu_app_secret', compose['secrets'])

    def test_invalid_source_lock_is_rejected_before_runtime_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            (source / 'image-inputs.json').write_text(json.dumps(self.inputs))
            runtime = Path(directory) / 'runtime'
            for invalid in ([], {'schema_version': 2}, {'schema_version': 1, 'components': []},
                            {'schema_version': 1, 'components': {}},
                            *({'schema_version': 1, 'components': self.components | {'headscale': {'image': image}}}
                              for image in (None, '', '-bad', 'bad\nINJECT=1'))):
                with self.subTest(lock=invalid), patch.object(configure, 'SOURCE_ROOT', source):
                    (source / 'versions.lock.yaml').write_text(yaml.safe_dump(invalid))
                    with self.assertRaises(ValueError):
                        configure.render(self.site, runtime)
                    self.assertFalse(runtime.exists())
            with patch.object(configure, 'SOURCE_ROOT', source):
                (source / 'versions.lock.yaml').write_text('components: [')
                with self.assertRaisesRegex(ValueError, 'Invalid source lock YAML'):
                    configure.render(self.site, runtime)
                self.assertFalse(runtime.exists())

    def test_host_injection_and_collision_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for bad in ('https://example.com', 'example.com\nEVIL=1', '*.example.com'):
                self.site['casdoor_host'] = bad
                with self.assertRaises(ValueError):
                    configure.render(self.site, directory)
            self.site['casdoor_host'] = self.site['headscale_host']
            with self.assertRaises(ValueError):
                configure.render(self.site, directory)
    def test_default_policy_denies_and_oidc_groups_are_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = configure.render(self.site, directory)
            self.assertEqual(json.loads((root / 'headscale/policy.json').read_text())['acls'], [])
            hs = json.loads((root / 'headscale/config.yaml').read_text())
            self.assertEqual(hs['oidc']['allowed_groups'], ['employees/tailnet-members'])
            self.assertTrue(hs['oidc']['only_start_if_oidc_is_available'])
            hp = json.loads((root / 'headplane/config.yaml').read_text())
            self.assertTrue(hp['oidc']['disable_api_key_login'])
            self.assertEqual(hp['server']['cookie_max_age'], 300)


class ExternalConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.runtime = self.base / 'runtime'
        self.site = json.loads((ROOT / 'deploy/site.external.example.json').read_text())
        self.hs_secret = self.base / 'hs-secret'
        self.hp_secret = self.base / 'hp-secret'
        self.hs_secret.write_text('registered-headscale-secret\n')
        self.hp_secret.write_text('registered-headplane-secret\n')

    def render(self, **kwargs):
        return configure.render(self.site, self.runtime,
                                headscale_client_secret_file=self.hs_secret,
                                headplane_client_secret_file=self.hp_secret, **kwargs)

    def snapshot(self):
        return {str(path.relative_to(self.runtime)): path.read_bytes()
                for path in self.runtime.rglob('*') if path.is_file()}

    def test_external_owns_only_tailnet_configuration(self):
        self.site['identity']['issuer'] = 'https://login.example.com/identity/'
        self.render()
        for name in ('casdoor', 'sync', 'secrets/db_password', 'secrets/feishu_app_secret'):
            self.assertFalse((self.runtime / name).exists(), name)
        for name, secret in (('headscale', 'registered-headscale-secret'),
                             ('headplane', 'registered-headplane-secret')):
            config = yaml.safe_load((self.runtime / name / 'config.yaml').read_text())
            self.assertEqual(config['oidc']['issuer'], self.site['identity']['issuer'])
            self.assertEqual((self.runtime / 'secrets' / f'{name}_oidc_secret').read_text(), secret)
        env = (self.runtime / 'compose.env').read_text()
        for name in ('CASDOOR_', 'POSTGRES_', 'WORKER_'):
            self.assertNotIn(name, env)
        for name in ('compose.yaml', 'compose.offline.yaml'):
            compose = yaml.safe_load((self.runtime / 'deploy' / name).read_text())
            self.assertEqual(set(compose['services']), {'headscale', 'headplane', 'proxy'})
            self.assertNotIn('casdoor', json.dumps(compose))
            self.assertNotIn('db_password', json.dumps(compose))
        proxy = yaml.safe_load((self.runtime / 'deploy/compose.yaml').read_text())['services']['proxy']
        self.assertIn('./Caddyfile:/etc/caddy/Caddyfile:ro', proxy['volumes'])
        self.assertNotIn('CASDOOR_HOST', (self.runtime / 'deploy/Caddyfile').read_text())
        self.assertNotIn('login.example.com', (self.runtime / 'deploy/Caddyfile').read_text())

    def test_descriptor_binds_generated_files_without_binding_operator_state(self):
        import hashlib
        self.render()
        descriptor = json.loads((self.runtime / 'deployment.json').read_text())
        self.assertEqual(descriptor['identity'], self.site['identity'])
        self.assertEqual(descriptor['directory_sync'], {'owner': 'external'})
        self.assertEqual(set(descriptor['configuration_sha256']),
                         {'deploy/compose.yaml', 'deploy/compose.offline.yaml', 'deploy/Caddyfile'})
        for name, digest in descriptor['configuration_sha256'].items():
            self.assertEqual(hashlib.sha256((self.runtime / name).read_bytes()).hexdigest(), digest)
        self.assertNotIn('registered-headscale-secret', json.dumps(descriptor))

    def test_missing_invalid_or_shared_credentials_fail_before_writes(self):
        with self.assertRaisesRegex(ValueError, 'requires an existing'):
            configure.render(self.site, self.runtime)
        self.assertFalse(self.runtime.exists())
        for value in ('', ' ', 'invalid\nsecret', self.hs_secret.read_text()):
            with self.subTest(secret=value):
                self.hp_secret.write_text(value)
                with self.assertRaises(ValueError):
                    self.render()
                self.assertFalse(self.runtime.exists())

    def test_rerender_preserves_credentials_and_rejects_rotation(self):
        self.render()
        before = self.snapshot()
        configure.render(self.site, self.runtime)
        self.assertEqual(self.snapshot(), before)
        self.hs_secret.write_text('replacement-secret')
        with self.assertRaisesRegex(ValueError, 'rotation is separate'):
            self.render()
        self.assertEqual(self.snapshot(), before)

    def test_identity_changes_fail_without_partial_writes(self):
        self.render()
        before = self.snapshot()
        for identity in ({'mode': 'external', 'issuer': 'https://another.example.com'},
                         {'mode': 'bundled'}):
            self.site['identity'] = identity
            self.site['casdoor_host'] = 'login.example.com'
            with self.assertRaisesRegex(ValueError, 'migration required'):
                self.render()
            self.assertEqual(self.snapshot(), before)

    def test_legacy_runtime_cannot_switch_mode_or_issuer(self):
        bundled = json.loads((ROOT / 'deploy/site.example.json').read_text())
        configure.render(bundled, self.runtime)
        (self.runtime / 'deployment.json').unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Legacy runtime is bundled'):
            self.render()
        bundled['casdoor_host'] = 'another.example.com'
        with self.assertRaisesRegex(ValueError, 'issuer differs'):
            configure.render(bundled, self.runtime)
        self.assertEqual(self.snapshot(), before)

    def test_directory_ownership_controls_worker_only(self):
        for owner in ('local', 'external', 'disabled'):
            with self.subTest(owner=owner):
                self.runtime = self.base / owner
                self.site['directory_sync'] = {'owner': owner}
                self.render()
                compose = yaml.safe_load((self.runtime / 'deploy/compose.yaml').read_text())
                offline = yaml.safe_load((self.runtime / 'deploy/compose.offline.yaml').read_text())
                self.assertEqual('worker' in compose['services'], owner == 'local')
                self.assertEqual('worker' in offline['services'], owner == 'local')
                self.assertEqual((self.runtime / 'sync/state').exists(), owner == 'local')
                self.assertNotIn('casdoor', compose['services'])
                self.assertFalse((self.runtime / 'sync/sync.json').exists())
                if owner == 'local':
                    self.assertEqual(compose['services']['worker']['profiles'], ['sync'])

    def test_invalid_descriptor_fails_before_writes(self):
        self.render()
        path = self.runtime / 'deployment.json'
        descriptor = json.loads(path.read_text())
        descriptor['services'].append('casdoor')
        path.write_text(json.dumps(descriptor))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.render()
        self.assertEqual(self.snapshot(), before)

    def test_external_does_not_require_bundled_image_catalog_entries(self):
        import shutil
        source = self.base / 'source'
        shutil.copytree(ROOT / 'deploy', source / 'deploy')
        lock = yaml.safe_load((ROOT / 'versions.lock.yaml').read_text())
        del lock['components']['casdoor']
        (source / 'versions.lock.yaml').write_text(yaml.safe_dump(lock))
        inputs = json.loads((ROOT / 'image-inputs.json').read_text())
        for name in ('database', 'sync'):
            inputs['images'].pop(name)
        (source / 'image-inputs.json').write_text(json.dumps(inputs))
        with patch.object(configure, 'SOURCE_ROOT', source):
            self.render()

    def test_bundled_generated_topology_preserves_legacy_services(self):
        bundled = json.loads((ROOT / 'deploy/site.example.json').read_text())
        configure.render(bundled, self.runtime)
        for name in ('compose.yaml', 'compose.offline.yaml'):
            expected = yaml.safe_load((ROOT / 'deploy' / name).read_text())
            actual = yaml.safe_load((self.runtime / 'deploy' / name).read_text())
            self.assertEqual(actual, expected)
        self.assertEqual((self.runtime / 'deploy/Caddyfile').read_text(),
                         (ROOT / 'deploy/Caddyfile').read_text())
