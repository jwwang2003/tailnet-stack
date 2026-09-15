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
        for option in ['--region', '--organization', '--tag', '--dry-run']:
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

    def run_upload(self, *, region='cn-north-4', platform='linux/amd64',
                   missing=False, push_failure=False, bad_digest=False,
                   dry_run=False, credentials=True, registry=None):
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
                    image_id = ids.setdefault(ref, 'sha256:' + str(len(ids) + 1) * 64)
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
            for name in ['versions.lock.yaml', 'image-inputs.json']:
                shutil.copy(ROOT / name, root / name)
            env = {'PATH': os.environ.get('PATH', ''), 'SWR_ORG': 'test-org'}
            if credentials:
                env.update(HUAWEI_AK='test-ak', HUAWEI_SK='test-secret')
            if registry:
                env['SWR_REGISTRY'] = registry
            with patch.dict(os.environ, env, clear=True), patch.object(swr, 'ROOT', root), \
                    patch.object(swr.subprocess, 'run', side_effect=command):
                result = CliRunner().invoke(swr.app, ['--region', region] + (['--dry-run'] if dry_run else []))
            digests = {p.name: p.read_text() for p in root.glob('.runtime/swr-digests/**/*.txt')}
            return result.exit_code, calls, digests, result.output

    def test_region_login_six_images_and_registry_digests(self):
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
                self.assertNotIn('All six images uploaded', output)


if __name__ == '__main__':
    unittest.main()
