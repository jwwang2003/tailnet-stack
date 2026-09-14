import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('configure', ROOT / 'scripts/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)

class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.site = json.loads((ROOT / 'deploy/site.example.json').read_text())
    def test_rerender_preserves_credentials_policy_and_digest_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configure.render(self.site, root)
            secret = (root / 'secrets/cookie_secret').read_text()
            self.assertEqual(len(secret), 32)
            policy = root / 'headscale/policy.json'
            policy.write_text('{"acls": [{"action":"accept"}]}')
            env = root / 'compose.env'
            env.write_text(env.read_text().replace('caddy:2.10.2-alpine', 'caddy@sha256:' + 'a'*64))
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
                                   ('casdoor', 'CASDOOR_IMAGE'), ('sync', 'WORKER_IMAGE')):
                self.assertEqual(fresh[key], f'tailnet/{component}:2026.09-rc.2')
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
