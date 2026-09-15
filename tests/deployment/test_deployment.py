import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from deployment import (deployment_from_site, load_deployment, make_deployment,
                        selected_products, validate_deployment, validate_issuer,
                        deployment_for_config, verify_configuration)


class DeploymentTests(unittest.TestCase):
    def test_selection(self):
        legacy = load_deployment()
        self.assertEqual(set(legacy['artifacts']), {'headscale', 'headplane', 'casdoor', 'database', 'sync', 'reverse_proxy'})
        external = deployment_from_site({'identity': {'mode': 'external', 'issuer': 'https://id.example.com/oidc/'}})
        self.assertEqual(external['identity']['issuer'], 'https://id.example.com/oidc/')
        self.assertEqual(external['directory_sync']['owner'], 'external')
        self.assertEqual(external['services'], ['headscale', 'headplane', 'proxy'])
        self.assertEqual(selected_products(external), ('headscale', 'headplane'))
        local = make_deployment('external', 'https://id.example.com', 'local')
        self.assertIn('worker', local['services'])
        self.assertNotIn('db', local['services'])

    def test_invalid_issuers(self):
        for issuer in [None, '', 'http://id.example.com', 'https://u:p@id.example.com',
                       'https://id.example.com?', 'https://id.example.com#',
                       'https://id.example.com:bad', 'https://id.example.com:0',
                       'https://id.example.com/path with space', 'https://id.example.com\\evil']:
            with self.subTest(issuer=issuer), self.assertRaises(ValueError):
                validate_issuer(issuer)

    def test_site_and_descriptor_conflicts(self):
        with self.assertRaises(ValueError):
            deployment_from_site({'casdoor_host': 'id.example.com', 'identity': {'issuer': 'https://other.example.com'}})
        for mode, owner in [('unknown', 'local'), ('external', 'unknown')]:
            with self.assertRaises(ValueError):
                make_deployment(mode, 'https://id.example.com', owner)
        value = make_deployment('external', 'https://id.example.com')
        value['services'].append('casdoor')
        with self.assertRaises(ValueError):
            validate_deployment(value)

    def test_explicit_descriptor_cannot_fall_back_to_bundled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deployment.json'
            with self.assertRaises(FileNotFoundError):
                load_deployment(path)
            value = make_deployment('external', 'https://id.example.com')
            path.write_text(json.dumps(value))
            self.assertEqual(load_deployment(path), value)
            value['artifacts'].append('database')
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_deployment(path)

    def test_runtime_metadata_is_required_and_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'sync' / 'sync.json'
            config.parent.mkdir()
            config.write_text('{}')
            value = make_deployment('external', 'https://id.example.com')
            descriptor = root / 'deployment.json'
            descriptor.write_text(json.dumps(value))
            self.assertEqual(deployment_for_config(config), value)
            bundled = root / 'bundled.json'
            bundled.write_text(json.dumps(make_deployment()))
            with self.assertRaisesRegex(ValueError, 'differs'):
                deployment_for_config(config, bundled)
            descriptor.unlink()
            (root / 'deploy').mkdir()
            (root / 'deploy/compose.yaml').write_text('services: {}')
            with self.assertRaisesRegex(ValueError, 'requires deployment.json'):
                deployment_for_config(config)
            descriptor.symlink_to(root / 'absent')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                load_deployment(descriptor)

    def test_empty_configuration_bindings_are_not_a_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'bind all'):
                verify_configuration(directory, make_deployment('external', 'https://id.example.com'))
