"""Check the SSH boundary without opening real tunnels or requiring credentials."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/remote-proxy-shell.sh'

class RemoteProxyShellTests(unittest.TestCase):
    def invoke(self, args):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'ssh'
            fake.write_text('#!/usr/bin/env python3\nimport json,os,sys\nopen(os.environ["SSH_ARGS"],"w").write(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            capture = root / 'args.json'
            result = subprocess.run(['bash', str(SCRIPT), *args], text=True, capture_output=True,
                env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'SSH_ARGS': str(capture)})
            return result, json.loads(capture.read_text()) if capture.exists() else None

    def test_default_reverse_tunnel_and_session_environment(self):
        result, args = self.invoke(['wjw@server.example.com'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args[args.index('-R') + 1], '127.0.0.1:17890:127.0.0.1:7890')
        self.assertIn('ExitOnForwardFailure=yes', args)
        self.assertIn('ControlPath=none', args)
        command = args[-1]
        for key in ['http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']:
            self.assertIn(key + '=http://127.0.0.1:17890', command)
        self.assertIn('no_proxy=localhost,127.0.0.1,::1', command)
        self.assertTrue(command.endswith('bash -l'))
        self.assertNotIn('-g', args)

    def test_custom_ports_and_ipv6_destination(self):
        result, args = self.invoke(['--local-port', '07890', '--remote-port', '17891', '--ssh-port', '2222', 'wjw@[::1]'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args[args.index('-R') + 1], '127.0.0.1:17891:127.0.0.1:7890')
        self.assertEqual(args[args.index('-p') + 1], '2222')
        self.assertIn('http_proxy=http://127.0.0.1:17891', args[-1])

    def test_bad_arguments_never_execute_ssh(self):
        for args in [[], ['--remote-port'], ['--remote-port', '0', 'host'],
                     ['--remote-port', '65536', 'host'], ['--remote-port', '12;id', 'host'],
                     ['--remote-port', '999999999999', 'host'], ['host;id'], ['host', 'extra'], ['-N', 'host']]:
            with self.subTest(args=args):
                result, captured = self.invoke(args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(captured)
