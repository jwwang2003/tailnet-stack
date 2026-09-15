import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]


class RestoreSafety(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.lock = self.base / "versions.lock.yaml"
        self.lock.write_bytes(b"schema_version: 1\n")
        self.destination = self.base / "new-restore"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "docker"
        fake.write_text("#!/bin/sh\nif [ \"$1\" = ps ]; then exit 0; fi\nif [ \"$1\" = volume ]; then exit 1; fi\nexit 99\n")
        fake.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def backup(self, extra=None, lock=None):
        lock = self.lock.read_bytes() if lock is None else lock
        dump = b"fixture database dump"
        metadata = {"schema_version": 1, "versions_lock_sha256": hashlib.sha256(lock).hexdigest(), "database_dump_sha256": hashlib.sha256(dump).hexdigest()}
        files = {
            "metadata.json": json.dumps(metadata).encode(), "versions.lock.yaml": lock,
            "database.dump": dump, "runtime/compose.env": b"RUNTIME_DIR=/old/path\nRUN_UID=10\nRUN_GID=10\n",
            "deploy/compose.yaml": b"name: fixture\n", "deploy/Caddyfile": b"# fixture\n",
        }
        path = self.base / "backup.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o600
                archive.addfile(member, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra)
        return path

    def run_restore(self, backup):
        return subprocess.run(["bash", str(ROOT / "scripts/restore.sh"), str(backup), str(self.lock), str(self.destination), "restore-test"], env=self.environment, text=True, capture_output=True)

    def test_existing_destination_never_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / "existing"
        marker.write_text("untouched")
        result = self.run_restore(self.backup())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "untouched")
        self.assertIn("must not exist", result.stderr)

    def test_mismatched_lock_refused_before_extract(self):
        result = self.run_restore(self.backup(lock=b"different release\n"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release lock", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_archive_links_and_traversal_refused(self):
        traversal = tarfile.TarInfo("../escaped")
        link = tarfile.TarInfo("runtime/escape")
        link.type, link.linkname = tarfile.SYMTYPE, "/tmp"
        for member in (traversal, link):
            with self.subTest(member=member.name):
                result = self.run_restore(self.backup(extra=member))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsafe archive", result.stderr)
                self.assertFalse(self.destination.exists())

    def test_safe_extract_relocates_runtime_and_project(self):
        result = self.run_restore(self.backup())
        # Docker is deliberately mocked: fail at Compose config after filesystem restoration.
        self.assertEqual(result.returncode, 99, result.stderr)
        env = self.destination / "runtime/compose.env"
        self.assertIn("RUNTIME_DIR=" + str(self.destination / "runtime"), env.read_text())
        self.assertIn("COMPOSE_PROJECT_NAME=restore-test", env.read_text())
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_backup_preflight_rejects_wrong_runtime_and_links(self):
        runtime = self.base / "runtime"
        runtime.mkdir()
        env = runtime / "compose.env"
        env.write_text("RUNTIME_DIR=/wrong/path\n")
        archive = self.base / "new-backup.tar.gz"
        command = ["bash", str(ROOT / "scripts/backup.sh"), str(runtime), str(archive)]
        result = subprocess.run(command, env=self.environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUNTIME_DIR differs", result.stderr)
        self.assertFalse(archive.exists())
        env.write_text("RUNTIME_DIR=" + str(runtime) + "\n")
        (runtime / ".maintenance.lock").symlink_to(self.lock)
        result = subprocess.run(command, env=self.environment, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("link or special file", result.stderr)
        self.assertEqual(self.lock.read_bytes(), b"schema_version: 1\n")
        self.assertFalse(archive.exists())


class DeploymentRecovery(unittest.TestCase):
    backup = RestoreSafety.backup
    run_restore = RestoreSafety.run_restore

    def setUp(self):
        RestoreSafety.setUp(self)
        self.trace = self.base / 'docker-trace.jsonl'
        self.environment['DOCKER_TRACE'] = str(self.trace)
        self.environment['RUNNING_SERVICES'] = 'headscale\nproxy\ncasdoor\nworker\n'
        fake = self.bin / 'docker'
        fake.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_TRACE'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args[0] == 'ps':
    sys.exit(0)
if args[0] == 'volume':
    sys.exit(1)
if os.environ.get('FAIL_ON') in args:
    sys.exit(42)
if 'ps' in args and '--services' in args:
    print(os.environ['RUNNING_SERVICES'])
if 'ps' in args and '--quiet' in args:
    print('proxy-container')
if 'pg_dump' in args:
    print('database fixture')
''')
        fake.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def runtime(self, mode='external', owner='external', legacy=False):
        import importlib.util
        spec = importlib.util.spec_from_file_location('recovery_deployment_test', ROOT / 'scripts/deployment.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        runtime = self.base / 'runtime'
        runtime.mkdir()
        files = {
            'compose.env': 'RUNTIME_DIR=' + str(runtime) + '\nCASDOOR_HOST=identity.example.com\n',
            'headscale/config.yaml': 'oidc: {issuer: https://identity.example.com}\n',
            'headplane/config.yaml': 'oidc: {issuer: https://identity.example.com}\n',
            'headscale/data/db.sqlite': 'tailnet database',
            'headscale/data/noise_private.key': 'tailnet private key',
            'headplane/data/state': 'sessions',
            'casdoor/app.conf': 'identity database password',
            'casdoor/files/signing.pem': 'identity private key',
            'secrets/db_password': 'identity password',
            'secrets/headscale_oidc_secret': 'headscale client',
            'secrets/headplane_oidc_secret': 'headplane client',
            'secrets/casdoor_sync_client_secret': 'sync credential',
            'secrets/unrelated_identity_secret': 'unowned credential',
            'sync/sync.json': json.dumps({'casdoor': {'client_secret_file': '/run/secrets/casdoor_sync_client_secret'}}),
            'sync/state/journal.json': 'revocation state',
        }
        for name, content in files.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if not legacy:
            deployment = module.make_deployment(mode, 'https://identity.example.com', owner)
            config_spec = importlib.util.spec_from_file_location('recovery_config_fixture', ROOT / 'scripts/configure.py')
            configure = importlib.util.module_from_spec(config_spec)
            config_spec.loader.exec_module(configure)
            configs = configure.deployment_files(deployment)
            for name, content in configs.items():
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            deployment['configuration_sha256'] = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in configs.items()}
            (runtime / 'deployment.json').write_text(json.dumps(deployment))
        return runtime

    def run_backup(self, runtime):
        archive = self.base / 'new-backup.tar.gz'
        result = subprocess.run(['bash', str(ROOT / 'scripts/backup.sh'), str(runtime), str(archive)], env=self.environment, text=True, capture_output=True)
        return result, archive

    def restore_archive(self, archive, mode=None):
        command = ['bash', str(ROOT / 'scripts/restore.sh'), str(archive), str(ROOT / 'versions.lock.yaml'), str(self.destination), 'restore-test']
        if mode:
            command += ['--expected-mode', mode]
        return subprocess.run(command, env=self.environment, text=True, capture_output=True)

    def rewrite_archive(self, archive, change):
        with tarfile.open(archive) as stream:
            files = {member.name.removeprefix('./'): stream.extractfile(member).read() for member in stream.getmembers() if member.isfile()}
        change(files)
        with tarfile.open(archive, 'w:gz') as stream:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o600
                stream.addfile(member, io.BytesIO(data))

    def test_external_round_trip_excludes_identity_and_nonlocal_worker(self):
        runtime = self.runtime()
        result, archive = self.run_backup(runtime)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(archive) as stream:
            names = {member.name.removeprefix('./') for member in stream.getmembers()}
            self.assertIn('runtime/headscale/data/noise_private.key', names)
            self.assertIn('runtime/headplane/data/state', names)
            self.assertNotIn('database.dump', names)
            self.assertFalse(any(name.startswith('runtime/casdoor') or name.startswith('runtime/sync') for name in names))
            self.assertNotIn('runtime/secrets/db_password', names)
            self.assertNotIn('runtime/secrets/casdoor_sync_client_secret', names)
            metadata = json.load(stream.extractfile('./metadata.json'))
            self.assertEqual(metadata['schema_version'], 2)
            self.assertEqual(metadata['deployment']['identity']['mode'], 'external')
        backup_calls = self.calls()
        stop = next(call for call in backup_calls if 'stop' in call)
        self.assertEqual(stop[stop.index('stop') + 3:], ['headplane', 'headscale', 'proxy'])
        start = next(call for call in backup_calls if 'start' in call)
        self.assertEqual(start[start.index('start') + 1:], ['headscale', 'proxy'])
        result = self.restore_archive(archive, 'external')
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls():
            self.assertFalse(set(call) & {'casdoor', 'db', 'worker', 'pg_dump', 'pg_restore', 'up'})
            self.assertFalse(any('casdoor_db' in arg for arg in call))
        self.assertEqual((self.destination / 'runtime/headscale/data/db.sqlite').read_text(), 'tailnet database')
        self.assertEqual(json.loads((runtime / 'deployment.json').read_text()), json.loads((self.destination / 'runtime/deployment.json').read_text()))
        self.assertIn(str(self.destination / 'runtime/deploy/compose.yaml'), self.calls()[-1])

    def test_external_local_worker_preserves_journal_and_its_credentials(self):
        result, archive = self.run_backup(self.runtime(owner='local'))
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(archive) as stream:
            names = {member.name.removeprefix('./') for member in stream.getmembers()}
            self.assertIn('runtime/sync/state/journal.json', names)
            self.assertIn('runtime/secrets/casdoor_sync_client_secret', names)
            self.assertNotIn('runtime/secrets/unrelated_identity_secret', names)
        self.trace.unlink()
        result = self.restore_archive(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('start' in call or 'up' in call or 'worker' in call for call in self.calls()))

    def test_legacy_new_backup_is_bundled_schema_two(self):
        result, archive = self.run_backup(self.runtime(legacy=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.restore_archive(archive, 'bundled')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('pg_dump' in call for call in self.calls()))
        self.assertTrue(any('pg_restore' in call for call in self.calls()))
        self.assertFalse(any('worker' in call and ('up' in call or 'create' in call) for call in self.calls()))

    def test_schema_one_bundled_restore_still_runs_database_only(self):
        result = self.run_restore(self.backup())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any('pg_restore' in call for call in self.calls()))
        self.assertEqual([call[call.index('up') + 1:] for call in self.calls() if 'up' in call], [['--detach', 'db']])

    def test_external_archive_expected_mode_mismatch_has_no_side_effects(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        result = self.restore_archive(archive, 'bundled')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected-mode', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])

    def test_archive_descriptor_and_configuration_mismatch_rejected(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        self.rewrite_archive(archive, lambda files: files.update({'runtime/deploy/Caddyfile': b'altered routes'}))
        result = self.restore_archive(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum differs', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])

    def test_external_archive_rejects_leftover_identity_state(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        self.rewrite_archive(archive, lambda files: files.update({'runtime/casdoor/app.conf': b'identity password'}))
        result = self.restore_archive(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unowned', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])

    def test_invalid_runtime_descriptor_never_falls_back(self):
        runtime = self.runtime()
        descriptor = runtime / 'deployment.json'
        original = descriptor.read_text()
        for bad in ('{', None, original.replace('headplane', 'casdoor')):
            with self.subTest(descriptor=bad):
                if bad is None:
                    descriptor.unlink()
                else:
                    descriptor.write_text(bad)
                result, archive = self.run_backup(runtime)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(archive.exists())
                self.assertEqual(self.calls(), [])
                descriptor.write_text(original)

    def test_copy_failure_restarts_only_previously_running_owned_writers(self):
        self.environment['FAIL_ON'] = 'cp'
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertFalse(archive.exists())
        restart = self.calls()[-1]
        self.assertEqual(restart[restart.index('start') + 1:], ['headscale', 'proxy'])
        self.assertEqual(list(self.base.glob('*.partial.*')), [])

    def test_descriptor_and_archive_metadata_must_agree(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        def change(files):
            descriptor = json.loads(files['runtime/deployment.json'])
            descriptor['identity']['issuer'] = 'https://different.example.com'
            files['runtime/deployment.json'] = json.dumps(descriptor).encode()
        self.rewrite_archive(archive, change)
        result = self.restore_archive(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('descriptor differs', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])

    def test_external_archive_cannot_smuggle_database_dump(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        self.rewrite_archive(archive, lambda files: files.update({'database.dump': b'identity database'}))
        result = self.restore_archive(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity database dump', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])

    def test_external_effective_compose_cannot_reintroduce_identity_service(self):
        runtime = self.runtime()
        compose = runtime / 'deploy/compose.yaml'
        config = yaml.safe_load(compose.read_text())
        config['services']['casdoor'] = {'image': 'fixture'}
        compose.write_text(yaml.safe_dump(config))
        descriptor = runtime / 'deployment.json'
        data = json.loads(descriptor.read_text())
        data['configuration_sha256']['deploy/compose.yaml'] = hashlib.sha256(compose.read_bytes()).hexdigest()
        descriptor.write_text(json.dumps(data))
        result, archive = self.run_backup(runtime)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Compose services differ', result.stderr)
        self.assertFalse(archive.exists())
        self.assertEqual(self.calls(), [])

    def test_stop_failure_still_restarts_original_owned_writers(self):
        self.environment['FAIL_ON'] = 'stop'
        result, archive = self.run_backup(self.runtime(owner='local'))
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertFalse(archive.exists())
        restart = self.calls()[-1]
        self.assertEqual(restart[restart.index('start') + 1:], ['headscale', 'proxy', 'worker'])


    def test_archive_cannot_restore_into_shared_volumes_or_bind_mounts(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        original = archive.read_bytes()
        for mutation in ('external_volume', 'named_volume', 'driver_volume', 'bind_mount',
                         'long_mount', 'include', 'extends', 'volumes_from'):
            with self.subTest(mutation=mutation):
                archive.write_bytes(original)
                self.trace.unlink(missing_ok=True)
                def change(files):
                    name = 'deploy/compose.yaml'
                    compose = yaml.safe_load(files['runtime/' + name])
                    proxy = compose['services']['proxy']
                    if mutation == 'external_volume':
                        compose['volumes']['caddy_data'] = {'external': True, 'name': 'shared_casdoor_db'}
                    elif mutation == 'named_volume':
                        compose['volumes']['caddy_data'] = {'name': 'shared_casdoor_db'}
                    elif mutation == 'driver_volume':
                        compose['volumes']['caddy_data'] = {'driver': 'local', 'driver_opts': {'device': '/shared/identity'}}
                    elif mutation == 'bind_mount':
                        proxy['volumes'][1] = '/shared/identity:/data'
                    elif mutation == 'long_mount':
                        proxy['volumes'][1] = {'type': 'bind', 'source': '/shared/identity', 'target': '/data'}
                    elif mutation == 'include':
                        compose['include'] = ['/shared/identity/compose.yaml']
                    elif mutation == 'extends':
                        proxy['extends'] = {'file': '/shared/identity/compose.yaml', 'service': 'casdoor'}
                    else:
                        proxy['volumes_from'] = ['container:shared-casdoor']
                    files['runtime/' + name] = yaml.safe_dump(compose).encode()
                    descriptor = json.loads(files['runtime/deployment.json'])
                    descriptor['configuration_sha256'][name] = hashlib.sha256(files['runtime/' + name]).hexdigest()
                    files['runtime/deployment.json'] = json.dumps(descriptor).encode()
                    metadata = json.loads(files['metadata.json'])
                    metadata['deployment'] = descriptor
                    files['metadata.json'] = json.dumps(metadata).encode()
                self.rewrite_archive(archive, change)
                result = self.restore_archive(archive)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.calls(), [])

    def test_runtime_and_archived_issuer_must_match_descriptor(self):
        runtime = self.runtime()
        result, archive = self.run_backup(runtime)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.trace.unlink()
        changed = b'oidc: {issuer: https://wrong.example.com}\n'
        self.rewrite_archive(archive, lambda files: files.update({'runtime/headscale/config.yaml': changed}))
        result = self.restore_archive(archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('issuer differs', result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls(), [])
        archive.unlink()
        (runtime / 'headscale/config.yaml').write_bytes(changed)
        result, archive = self.run_backup(runtime)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('issuer differs', result.stderr)
        self.assertFalse(archive.exists())
        self.assertEqual(self.calls(), [])

    def test_legacy_backup_restores_a_rerenderable_issuer(self):
        import importlib.util
        import shutil
        spec = importlib.util.spec_from_file_location('legacy_recovery_config', ROOT / 'scripts/configure.py')
        configure = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(configure)
        site = json.loads((ROOT / 'deploy/site.example.json').read_text())
        runtime = self.base / 'runtime'
        configure.render(site, runtime)
        (runtime / 'deployment.json').unlink()
        shutil.rmtree(runtime / 'deploy')
        result, archive = self.run_backup(runtime)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.restore_archive(archive, 'bundled')
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = self.destination / 'runtime'
        descriptor = json.loads((restored / 'deployment.json').read_text())
        self.assertEqual(descriptor['identity']['issuer'], 'https://' + site['casdoor_host'])
        configure.render(site, restored)
        self.assertIn('COMPOSE_PROJECT_NAME=restore-test', (restored / 'compose.env').read_text())


    def test_restore_rebases_exported_runtime_variables_too(self):
        result, archive = self.run_backup(self.runtime())
        self.assertEqual(result.returncode, 0, result.stderr)
        def change(files):
            files['runtime/compose.env'] += b'export RUNTIME_DIR=/shared/identity\n RUN_UID = 9999\n'
        self.rewrite_archive(archive, change)
        result = self.restore_archive(archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = (self.destination / 'runtime/compose.env').read_text()
        self.assertNotIn('/shared/identity', env)
        self.assertNotIn('9999', env)
        self.assertNotIn('export RUNTIME_DIR', env)



if __name__ == "__main__":
    unittest.main()
