"""Exercise build command wiring with fake Docker; no daemon/source checkouts required."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/build-products.sh'

class BuildProxyTests(unittest.TestCase):
    def run_build(self, proxy=None, endpoint='unix:///var/run/docker.sock', driver='docker', rootless=False, platform=None, podman=False, buildx=True, engine=True, deployment=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy(SCRIPT, root / 'scripts/build-products.sh')
            shutil.copy(SCRIPT.parent / 'prepare-podman-buildfile.py', root / 'scripts/prepare-podman-buildfile.py')
            shutil.copy(SCRIPT.parent / 'deployment.py', root / 'scripts/deployment.py')
            for recipe in ('build/headscale.Dockerfile','build/sync.Dockerfile','headplane/Dockerfile','casdoor/Dockerfile'):
                path = root / recipe
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('FROM golang:1.26.5 AS builder\nFROM alpine:3.22\n')
            (root / 'scripts/release.py').write_text('import sys\nfrom deployment import load_deployment\nselection=load_deployment(sys.argv[sys.argv.index("--deployment")+1] if "--deployment" in sys.argv else None)\nif "artifacts" in sys.argv: print("\\n".join(selection["artifacts"]))\nelif "field" in sys.argv:\n print("test/image:rc" if sys.argv[-1] == "image" else "a"*40)\nelif "platform" in sys.argv: print("linux/amd64")\nelif "support-image" in sys.argv: print("test/sync:rc")\n')
            binary = root / 'bin'
            binary.mkdir()
            identity = binary / 'id'
            identity.write_text('#!/bin/sh\necho 1000\n')
            identity.chmod(0o755)
            git = binary / 'git'
            git.write_text('#!/bin/sh\nprintf \"' + 'b'*40 + '\\n\"\n')
            git.chmod(0o755)
            docker = binary / 'docker'
            docker.write_text('#!' + sys.executable + '\n' + '''import os,sys,json
args=sys.argv[1:]
if args[0]=='unshare': print('0 1000 1\\n1 165536 65536')
elif args[:2]==['buildx','version']:
 print('buildx test')
 sys.exit(0 if os.environ['TEST_BUILDX']=='yes' else 1)
elif args[0]=='version':
 print('test-engine')
 sys.exit(0 if os.environ['TEST_ENGINE']=='yes' else 1)
elif args[:2]==['context','inspect']: print(os.environ['TEST_ENDPOINT'])
elif args[:2]==['buildx','inspect']: print(os.environ['TEST_DRIVER'])
elif args[0]=='info':
 print('linux' if args[-1]=='{{.OSType}}' else os.environ['TEST_SECURITY'])
elif args[0]=='build':
 with open(os.environ['TEST_LOG'],'a') as f:
  f.write(json.dumps({'args':args,'http_proxy':os.environ.get('http_proxy'),'HTTPS_PROXY':os.environ.get('HTTPS_PROXY'),'recipe':open(args[args.index('--file')+1]).read()})+'\\n')
else: sys.exit(9)
''')
            docker.chmod(0o755)
            if podman:
                docker.rename(binary / 'podman')
                docker.symlink_to(binary / 'podman')
            log = root / 'builds.jsonl'
            env = {k:v for k,v in os.environ.items() if k not in ('BUILD_PROXY_URL','DOCKER_HOST','DOCKER_CONTEXT','BUILD_PLATFORM','CONTAINER_ENGINE','DEPLOYMENT_FILE')}
            env.update(PATH=str(binary)+os.pathsep+env['PATH'], TEST_ENDPOINT=endpoint,
                       TEST_DRIVER=driver, TEST_BUILDX='yes' if buildx else 'no', TEST_ENGINE='yes' if engine else 'no', TEST_SECURITY='["name=rootless"]' if rootless else '[]', TEST_LOG=str(log))
            if deployment is not None:
                descriptor = root / 'deployment.json'
                descriptor.write_text(json.dumps(deployment))
                env['DEPLOYMENT_FILE'] = str(descriptor)
                (root / 'casdoor/Dockerfile').unlink()
            if platform is not None: env['BUILD_PLATFORM']=platform
            if proxy is not None: env['BUILD_PROXY_URL']=proxy
            result=subprocess.run(['bash',str(root / 'scripts/build-products.sh'),str(root)],env=env,text=True,capture_output=True)
            return result, [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_proxy_is_opt_in_and_all_four_builds_receive_it(self):
        result, builds=self.run_build('http://127.0.0.1:17890')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(builds),4)
        for build in builds:
            args=build['args']
            self.assertEqual(args[args.index('--network')+1], 'host')
            self.assertEqual(args[args.index('--builder')+1], 'default')
            self.assertEqual(args[args.index('--platform')+1], 'linux/amd64')
            self.assertIn('--load',args)
            self.assertIn('HTTPS_PROXY',args)
            self.assertIn('http_proxy',args)
            self.assertEqual(build['http_proxy'],'http://127.0.0.1:17890')
            self.assertEqual(build['HTTPS_PROXY'],'http://127.0.0.1:17890')

    def test_normal_build_network_is_unchanged(self):
        result, builds=self.run_build()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(builds),4)
        self.assertTrue(all('--network' not in build['args'] for build in builds))

    def test_explicit_target_platform_and_worker_revision(self):
        result, builds = self.run_build(platform='linux/arm64')
        self.assertEqual(result.returncode, 0, result.stderr)
        for build in builds:
            args = build['args']
            self.assertEqual(args[args.index('--platform')+1], 'linux/arm64')
        self.assertIn('org.opencontainers.image.revision=' + 'b'*40, builds[-1]['args'])
        result, builds = self.run_build(platform='windows/amd64')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(builds, [])

    def test_invalid_proxy_or_unreachable_builder_context_fails_before_build(self):
        cases=[{'proxy':'http://user:secret@127.0.0.1:17890'},
               {'proxy':'http://127.0.0.1:0'}, {'proxy':'http://127.0.0.1:65536'},
               {'proxy':'http://127.0.0.1:17890','endpoint':'ssh://other-host'},
               {'proxy':'http://127.0.0.1:17890','driver':'docker-container'},
               {'proxy':'http://127.0.0.1:17890','rootless':True}]
        for case in cases:
            with self.subTest(case=case):
                result, builds=self.run_build(**case)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(builds,[])

    def test_wrong_engine_or_missing_buildkit_stops_before_any_build(self):
        for settings, expected in [({'buildx':False}, 'Buildx is unavailable'),
                                   ({'engine':False}, 'engine is unreachable')]:
            with self.subTest(settings=settings):
                result, builds = self.run_build(**settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
                self.assertEqual(builds, [])

    def test_podman_symlink_selects_native_engine_and_qualified_recipes(self):
        result, builds = self.run_build(podman=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(builds), 4)
        for build in builds:
            self.assertNotIn('--load', build['args'])
            self.assertEqual(build['args'][build['args'].index('--ulimit')+1], 'nofile=65536:65536')
            self.assertEqual(build['args'][build['args'].index('--format')+1], 'docker')
            self.assertIn('FROM docker.io/library/', build['recipe'])

    def test_external_build_skips_identity_and_externally_owned_worker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('build_deployment', SCRIPT.parent / 'deployment.py')
        deployment = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deployment)
        for owner, count in [('external', 2), ('disabled', 2), ('local', 3)]:
            with self.subTest(owner=owner):
                result, builds = self.run_build(deployment=deployment.make_deployment(
                    'external', 'https://login.example.com', owner))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(builds), count)
                self.assertFalse(any('/casdoor/' in str(build['args']) for build in builds))
        result, builds = self.run_build(deployment={'schema_version': 99})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(builds, [])
