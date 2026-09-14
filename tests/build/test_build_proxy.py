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
    def run_build(self, proxy=None, endpoint='unix:///var/run/docker.sock', driver='docker', rootless=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy(SCRIPT, root / 'scripts/build-products.sh')
            (root / 'scripts/release.py').write_text('import sys\nif "field" in sys.argv:\n print("test/image:rc" if sys.argv[-1] == "image" else "a"*40)\n')
            binary = root / 'bin'
            binary.mkdir()
            docker = binary / 'docker'
            docker.write_text('#!' + sys.executable + '\n' + '''import os,sys,json
args=sys.argv[1:]
if args[:2]==['context','inspect']: print(os.environ['TEST_ENDPOINT'])
elif args[:2]==['buildx','inspect']: print(os.environ['TEST_DRIVER'])
elif args[0]=='info':
 print('linux' if args[-1]=='{{.OSType}}' else os.environ['TEST_SECURITY'])
elif args[0]=='build':
 with open(os.environ['TEST_LOG'],'a') as f:
  f.write(json.dumps({'args':args,'http_proxy':os.environ.get('http_proxy'),'HTTPS_PROXY':os.environ.get('HTTPS_PROXY')})+'\\n')
else: sys.exit(9)
''')
            docker.chmod(0o755)
            log = root / 'builds.jsonl'
            env = {k:v for k,v in os.environ.items() if k not in ('BUILD_PROXY_URL','DOCKER_HOST','DOCKER_CONTEXT')}
            env.update(PATH=str(binary)+os.pathsep+env['PATH'], TEST_ENDPOINT=endpoint,
                       TEST_DRIVER=driver, TEST_SECURITY='["name=rootless"]' if rootless else '[]', TEST_LOG=str(log))
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
            self.assertIn('HTTPS_PROXY',args)
            self.assertIn('http_proxy',args)
            self.assertEqual(build['http_proxy'],'http://127.0.0.1:17890')
            self.assertEqual(build['HTTPS_PROXY'],'http://127.0.0.1:17890')

    def test_normal_build_network_is_unchanged(self):
        result, builds=self.run_build()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(builds),4)
        self.assertTrue(all('--network' not in build['args'] for build in builds))

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
