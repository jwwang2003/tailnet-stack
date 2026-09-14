import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'scripts/sync-mirror-no-proxy.py'
spec = importlib.util.spec_from_file_location('mirror_proxy', PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class MirrorProxyTests(unittest.TestCase):
    def test_merges_all_mirrors_preserving_settings_and_effective_exclusions(self):
        config = {'registry-mirrors':['https://mra3ydig.mirror.aliyuncs.com', 'https://mirror.internal:5000/'],
                  'proxies':{'http-proxy':'http://127.0.0.1:17890','https-proxy':'http://127.0.0.1:17890','no-proxy':'old.internal'},
                  'log-driver':'json-file','data-root':'/srv/docker'}
        info = {'RegistryConfig':{'Mirrors':['https://mirror.internal:5000/','https://third.internal']},'NoProxy':'runtime.internal'}
        updated, hosts = m.merge(config,info,{'no_proxy':'shell.internal'})
        self.assertEqual(len(hosts),3)
        for host in (*hosts,'old.internal','runtime.internal','shell.internal','localhost'):
            self.assertIn(host,updated['proxies']['no-proxy'].split(','))
        self.assertEqual(updated['data-root'],config['data-root'])
        self.assertEqual(updated['log-driver'],config['log-driver'])
        self.assertEqual(updated['proxies']['http-proxy'],config['proxies']['http-proxy'])
        self.assertEqual(config['proxies']['no-proxy'],'old.internal')
        again,_ = m.merge(updated,info,{'no_proxy':'shell.internal'})
        self.assertEqual(updated,again)

    def test_empty_file_configuration_can_use_running_mirrors(self):
        updated,hosts=m.merge({}, {'RegistryConfig':{'Mirrors':['https://abc.mirror.aliyuncs.com/']}}, {})
        self.assertEqual(hosts,['abc.mirror.aliyuncs.com'])
        self.assertNotIn('http-proxy',updated['proxies'])

    def test_bad_mirrors_rejected(self):
        for value in ['not-a-url','https://user:password@mirror.local','https://*.mirror.local','https://mirror.local?q=x']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                m.merge({'registry-mirrors':[value]}, {}, {})

    def test_validation_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'daemon.json'
            original=b'{"log-driver":"json-file"}\n'
            path.write_bytes(original)
            updated,_=m.merge(json.loads(original),{}, {})
            with patch.object(m.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['dockerd'])):
                with self.assertRaises(subprocess.CalledProcessError):m.apply(path,original,updated)
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_success_writes_backup_and_only_requested_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'daemon.json'
            original=b'{"registry-mirrors":["https://mra3ydig.mirror.aliyuncs.com"]}\n'
            path.write_bytes(original)
            updated,_=m.merge(json.loads(original),{}, {})
            with patch.object(m.subprocess,'run') as validate:
                backup=m.apply(path,original,updated)
            self.assertEqual(backup.read_bytes(),original)
            self.assertEqual(backup.stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads(path.read_text()),updated)
            self.assertIn('--validate',validate.call_args.args[0])

    def test_concurrent_edit_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'daemon.json'
            path.write_bytes(b'{}')
            with patch.object(m.subprocess,'run',side_effect=lambda *a,**k:path.write_bytes(b'{"debug":true}')):
                with self.assertRaises(ValueError):m.apply(path,b'{}',{'proxies':{'no-proxy':'localhost'}})
            self.assertEqual(path.read_bytes(),b'{"debug":true}')
