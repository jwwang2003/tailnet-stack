import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
from typer.testing import CliRunner
from rich.console import Console

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('huawei_swr', ROOT / 'scripts/huawei-swr.py')
swr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swr)


class SWRTests(unittest.TestCase):
    def test_help_and_required_options(self):
        runner = CliRunner()
        result = runner.invoke(swr.app, ['--help'])
        self.assertEqual(result.exit_code, 0, result.output)
        for option in ['--region', '--organization', '--tag', '--image', '--platform', '--dry-run']:
            self.assertIn(option, result.output)
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(swr.subprocess, 'run') as docker:
                result = runner.invoke(swr.app, ['--dry-run'])
                self.assertEqual(result.exit_code, 2)
                docker.assert_not_called()

    def test_environment_options_and_explicit_region_precedence(self):
        env = {'SWR_REGION': 'cn-north-4', 'SWR_ORG': 'test-org'}
        with patch.dict(os.environ, env, clear=True), patch.object(swr.subprocess, 'run') as docker, \
                patch.object(swr, 'console', Console(width=240, markup=False)):
            for args, region in [([], 'cn-north-4'), (['--region', 'cn-east-3'], 'cn-east-3')]:
                result = CliRunner().invoke(swr.app, [*args, '--dry-run'], terminal_width=240)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(f'swr.{region}.myhuaweicloud.com/test-org/', result.output)
            docker.assert_not_called()

    def test_external_descriptor_filters_defaults_but_preserves_explicit_images(self):
        from deployment import make_deployment, DEPLOY_FILES
        with tempfile.TemporaryDirectory() as directory:
            descriptor = Path(directory) / 'deployment.json'
            for owner in ('external', 'disabled', 'local'):
                selection = make_deployment('external', 'https://login.example.com', owner)
                for name in DEPLOY_FILES:
                    path = Path(directory) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({'services': {service: {} for service in selection['services']}})
                                        if name.endswith('.yaml') else '# fixture proxy')
                    selection['configuration_sha256'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
                descriptor.write_text(json.dumps(selection))
                platform, sources = swr.load_sources(deployment=descriptor)
                expected = {'headscale', 'headplane', 'caddy'} | ({'sync'} if owner == 'local' else set())
                self.assertEqual(set(sources), expected)
                _, explicit = swr.load_sources(['api=local/api:v2'], 'linux/amd64', descriptor)
                self.assertEqual(explicit, {'api': 'local/api:v2'})
            descriptor.write_text('{}')
            with self.assertRaises(ValueError):
                swr.load_sources(deployment=descriptor)

    def run_upload(self, *, region='cn-north-4', platform='linux/amd64',
                   missing=False, push_failure=False, bad_digest=False,
                   dry_run=False, credentials=True, registry=None,
                   args=None, components=None, support_images=None, release_files=True):
        calls = []
        ids = {}
        targets = {}

        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1:3] == ['image', 'inspect']:
                ref = argv[3]
                if missing and len(ids) == 2:
                    raise subprocess.CalledProcessError(1, argv)
                if ref in targets:
                    image_id = targets[ref]
                    digests = [] if bad_digest else [ref.rsplit(':', 1)[0] + '@sha256:' + 'f' * 64]
                else:
                    image_id = ids.setdefault(ref, f'sha256:{len(ids) + 1:064x}')
                    digests = []
                data = [{'Id': image_id, 'Os': platform.split('/')[0],
                         'Architecture': platform.split('/')[1], 'RepoDigests': digests}]
                return subprocess.CompletedProcess(argv, 0, json.dumps(data))
            if argv[1] == 'tag':
                targets[argv[3]] = argv[2]
            if argv[1] == 'push' and push_failure:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if release_files:
                for name in ['versions.lock.yaml', 'image-inputs.json']:
                    shutil.copy(ROOT / name, root / name)
            if components is not None:
                (root / 'versions.lock.yaml').write_text(yaml.safe_dump({
                    'schema_version': 1, 'components': components,
                }))
            if support_images is not None:
                (root / 'image-inputs.json').write_text(json.dumps({
                    'schema_version': 1, 'platform': platform, 'images': support_images,
                }))
            env = {'PATH': os.environ.get('PATH', ''), 'SWR_ORG': 'test-org'}
            if credentials:
                env.update(HUAWEI_AK='test-ak', HUAWEI_SK='test-secret')
            if registry:
                env['SWR_REGISTRY'] = registry
            with patch.dict(os.environ, env, clear=True), patch.object(swr, 'ROOT', root), \
                    patch.object(swr.subprocess, 'run', side_effect=command):
                result = CliRunner().invoke(
                    swr.app,
                    ['--region', region, *(args or [])] + (['--dry-run'] if dry_run else []),
                )
            digests = {p.name: p.read_text() for p in root.glob('.runtime/swr-digests/**/*.txt')}
            return result.exit_code, calls, digests, result.output

    def test_region_login_default_images_and_registry_digests(self):
        code, calls, digests, output = self.run_upload()
        self.assertEqual(code, 0, output)
        login = next((argv, kw) for argv, kw in calls if argv[1] == 'login')
        self.assertEqual(login[0], ['docker', 'login', '--username', 'cn-north-4@test-ak',
                                   '--password-stdin', 'swr.cn-north-4.myhuaweicloud.com'])
        expected = hmac.new(b'test-secret', b'test-ak', hashlib.sha256).hexdigest()
        self.assertEqual(login[1]['input'], expected + '\n')
        self.assertNotIn(expected, output)
        self.assertNotIn('test-secret', output)
        for argv, kw in calls:
            self.assertNotIn('test-secret', argv)
            self.assertNotIn(expected, argv)
            self.assertNotIn('HUAWEI_SK', kw['env'])
        login_index = next(i for i, (a, _) in enumerate(calls) if a[1] == 'login')
        self.assertEqual(login_index, 6)
        pushes = [a for a, _ in calls if a[1] == 'push']
        self.assertEqual(len(pushes), 6)
        self.assertTrue(all(a[-1].startswith('swr.cn-north-4.myhuaweicloud.com/test-org/') for a in pushes))
        self.assertTrue(all(a[-1].endswith('-amd64') for a in pushes))
        self.assertEqual(set(digests), {'headscale.txt', 'headplane.txt', 'casdoor.txt',
                                      'sync.txt', 'postgres.txt', 'caddy.txt'})
        self.assertTrue(all(v == 'sha256:' + 'f' * 64 + '\n' for v in digests.values()))

    def test_missing_image_or_wrong_architecture_stops_before_login(self):
        for options in [{'missing': True}, {'platform': 'linux/arm64'}]:
            with self.subTest(options=options):
                code, calls, digests, _ = self.run_upload(**options)
                self.assertEqual(code, 1)
                self.assertTrue(all(a[1:3] == ['image', 'inspect'] for a, _ in calls))
                self.assertFalse(digests)

    def test_dry_run_needs_neither_credentials_nor_docker(self):
        code, calls, digests, output = self.run_upload(dry_run=True, credentials=False)
        self.assertEqual(code, 0, output)
        self.assertFalse(calls)
        self.assertFalse(digests)
        self.assertIn('SWR upload', output)
        self.assertIn('Preview only', output)
        for name in ['headscale', 'headplane', 'casdoor', 'sync', 'postgres', 'caddy']:
            self.assertIn(name, output)

    def test_mismatched_registry_and_missing_credentials_fail_before_docker(self):
        for options in [{'registry': 'swr.cn-east-3.myhuaweicloud.com'}, {'credentials': False}]:
            with self.subTest(options=options):
                code, calls, _, _ = self.run_upload(**options)
                self.assertEqual(code, 1)
                self.assertFalse(calls)

    def test_push_failure_or_missing_digest_does_not_report_success(self):
        for options in [{'push_failure': True}, {'bad_digest': True}]:
            with self.subTest(options=options):
                code, calls, digests, output = self.run_upload(**options)
                self.assertEqual(code, 1)
                self.assertEqual(len([a for a, _ in calls if a[1] == 'push']), 1)
                self.assertFalse(digests)
                self.assertNotRegex(output, r'Uploaded \d+ image')

    def test_custom_images_work_without_release_files(self):
        for count in [1, 2]:
            with self.subTest(count=count):
                args = ['--platform', 'linux/amd64']
                for index in range(count):
                    args += ['--image', f'app-{index}=local/app-{index}:v{index}']
                code, calls, digests, output = self.run_upload(args=args, release_files=False)
                self.assertEqual(code, 0, output)
                pushes = [a[-1] for a, _ in calls if a[1] == 'push']
                self.assertEqual(pushes, [
                    f'swr.cn-north-4.myhuaweicloud.com/test-org/app-{i}:v{i}-amd64'
                    for i in range(count)
                ])
                self.assertEqual(len(digests), count)
                self.assertIn(f'Uploaded {count} image(s).', output)
                self.assertIn(f'[{count}/{count}]', output)

    def test_release_discovery_accepts_different_components_and_support_images(self):
        components = {f'app-{i}': {'image': f'local/app-{i}:v1'} for i in range(5)}
        code, calls, digests, output = self.run_upload(
            components=components,
            support_images={'cache': 'redis:7', 'database': 'postgres:17'},
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(len([a for a, _ in calls if a[1] == 'push']), 7)
        self.assertEqual(set(digests), {*(f'app-{i}.txt' for i in range(5)), 'cache.txt', 'postgres.txt'})
        self.assertIn('Uploaded 7 image(s).', output)

    def test_tag_override_and_arm64_platform(self):
        code, calls, _, output = self.run_upload(
            platform='linux/arm64', release_files=False,
            args=['--platform', 'linux/arm64', '--tag', 'release-arm64',
                  '--image', 'api=local/api@sha256:' + 'a' * 64,
                  '--image', 'cache=redis'],
        )
        self.assertEqual(code, 0, output)
        pushes = [a[-1] for a, _ in calls if a[1] == 'push']
        self.assertEqual(len(pushes), 2)
        self.assertTrue(all(value.endswith(':release-arm64') for value in pushes))

    def test_invalid_or_duplicate_custom_images_fail_before_docker(self):
        cases = [
            ['--image', 'redis:7'],
            ['--image', '../escape=redis:7'],
            ['--image', 'cache='],
            ['--image', 'cache=redis:7', '--image', 'cache=redis:8'],
            ['--image', 'cache=redis'],
            ['--image', 'cache=redis@sha256:' + 'a' * 64],
        ]
        for args in cases:
            with self.subTest(args=args):
                code, calls, _, output = self.run_upload(
                    args=['--platform', 'linux/amd64', *args], release_files=False,
                )
                self.assertEqual(code, 1, output)
                self.assertFalse(calls)

    def test_empty_release_or_alias_collision_is_rejected(self):
        for components, support in [
            ({}, {}),
            ({'postgres': {'image': 'postgres:17'}}, {'database': 'postgres:18'}),
        ]:
            with self.subTest(components=components):
                code, calls, _, output = self.run_upload(
                    components=components, support_images=support,
                )
                self.assertEqual(code, 1, output)
                self.assertFalse(calls)


if __name__ == '__main__':
    unittest.main()
