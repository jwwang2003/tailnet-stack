#!/usr/bin/env python3
"""Validate generated Headscale config against a local mock OIDC issuer and isolated DB."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure', ROOT / 'scripts/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    args = parser.parse_args()
    binary = str(args.binary.resolve())
    with tempfile.TemporaryDirectory(prefix='tailnet-headscale-smoke-') as temp:
        root = configure.render(json.loads((ROOT / 'deploy/site.example.json').read_text()), temp)
        port = free_port()
        issuer = f'http://127.0.0.1:{port}/oidc'
        env = {**os.environ, 'MOCKOIDC_CLIENT_ID': 'headscale', 'MOCKOIDC_CLIENT_SECRET': 'smoke-only-secret',
               'MOCKOIDC_ADDR': '127.0.0.1', 'MOCKOIDC_PORT': str(port), 'MOCKOIDC_USERS': '[]'}
        # Only synthetic test credentials; suppress verbose mock request logging.
        with (root / 'mock.log').open('w') as log:
            mock = subprocess.Popen([binary, 'mockoidc'], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        with urlopen(issuer + '/.well-known/openid-configuration', timeout=1) as response:
                            assert json.load(response)['issuer'] == issuer
                        break
                    except OSError:
                        if mock.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('Mock issuer failed to start')
                        time.sleep(0.1)
                path = root / 'headscale/config.yaml'
                config = json.loads(path.read_text())
                data = root / 'headscale/data'
                config['noise']['private_key_path'] = str(data / 'noise.key')
                config['database']['sqlite']['path'] = str(data / 'db.sqlite')
                config['policy']['path'] = str(root / 'headscale/policy.json')
                config['unix_socket'] = str(data / 'headscale.sock')
                config['listen_addr'] = f'127.0.0.1:{free_port()}'
                config['metrics_listen_addr'] = ''
                config['disable_check_updates'] = True
                config['derp']['urls'] = []
                config['oidc']['issuer'] = issuer
                config['oidc'].pop('client_secret_path')
                config['oidc']['client_secret'] = 'smoke-only-secret'
                path.write_text(json.dumps(config))
                result = subprocess.run([binary, '-c', str(path), 'configtest'], capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError('Headscale rejected generated configuration: ' + result.stderr[-2500:])
                print('PASS: generated configuration, mock OIDC discovery, isolated Headscale initialization')
            finally:
                mock.terminate()
                try:
                    mock.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    mock.kill()
                    mock.wait()

if __name__ == '__main__':
    main()
