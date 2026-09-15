"""Exercise transfers against a fake Docker daemon; no registry or daemon required."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('image_bundle', ROOT / 'scripts/image-bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class FakeDocker:
    def __init__(self, images):
        self.images = images
        self.calls = []
        self.engines = []
        self.platform = {'OSType': 'linux', 'Architecture': 'x86_64'}
        self.bad_load = False
        self.fail_save = False

    def __call__(self, *args):
        if args[0] not in ('docker', 'podman'):
            return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        self.calls.append(args[1:])
        self.engines.append(args[0])
        command = args[1:3]
        if command == ('info', '--format'):
            return json.dumps(self.platform)
        if args[1] == 'pull':
            return ''
        if command == ('image', 'inspect'):
            image = self.images.get(args[3])
            if image is None:
                raise subprocess.CalledProcessError(1, args)
            image = copy.deepcopy(image)
            if args[0] == 'podman': image['Id'] = image['Id'].removeprefix('sha256:')
            return json.dumps([image])
        if command == ('image', 'save'):
            if self.fail_save:
                raise subprocess.CalledProcessError(1, args)
            output_pos = args.index('--output') + 1
            Path(args[output_pos]).write_text(json.dumps([self.images[image_id] for image_id in args[output_pos+1:]]))
            return ''
        if command == ('image', 'load'):
            for image in json.loads(Path(args[4]).read_text()):
                if self.bad_load:
                    image['Config']['Labels'][bundle.REVISION] = 'bad'
                self.images[image['Id']] = image
            return ''
        if command == ('image', 'tag'):
            self.images[args[4]] = self.images[args[3]]
            return ''
        raise AssertionError('Unexpected Docker command: ' + repr(args))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.root.mkdir()
        for name in ('versions.lock.yaml', 'image-inputs.json'):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        for args in (('init', '-q'), ('add', '.'), ('-c', 'user.name=Test', '-c',
                     'user.email=test@example.com', 'commit', '-qm', 'fixture')):
            subprocess.run(['git', '-C', str(self.root), *args], check=True)
        self.addCleanup(patch.stopall)
        patch.object(bundle, 'ROOT', self.root).start()
        self.output = Path(self.tmp.name) / 'transfer'
        self.runtime = Path(self.tmp.name) / 'runtime'
        self.runtime.mkdir()
        self.env = self.runtime / 'compose.env'
        self.original = b'# Preserve this comment\nRUN_UID=1000\nDB_SECRET=do-not-copy\nHEADSCALE_IMAGE=old\n'
        self.env.write_bytes(self.original)
        self.env.chmod(0o600)
        self.config = bundle.inputs()
        self.architecture = self.config['platform'].split('/')[1]
        self.foreign_architecture = 'arm64' if self.architecture == 'amd64' else 'amd64'
        self.export_args = SimpleNamespace(output=self.output, platform=self.config['platform'], pull_supporting_images=False)
        self.import_args = SimpleNamespace(action='import', bundle=self.output, runtime=self.runtime)
        import yaml
        components = yaml.safe_load((self.root / 'versions.lock.yaml').read_text())['components']
        sources = bundle.binding()
        refs = self.config['images'] | {key: value['image'] for key, value in components.items()}
        self.headscale_reference = refs['headscale']
        images = {}
        for number, key in enumerate(bundle.ENV, 1):
            revision = (components[key]['source_commit'] if key in components else
                        sources['integration_commit'] if key == 'sync' else 'upstream-label')
            image = dict(Id='sha256:' + str(number) * 64, Os='linux', Architecture=self.architecture,
                         Config={'Labels': {bundle.REVISION: revision}})
            images[refs[key]] = images[image['Id']] = image
        self.docker = FakeDocker(images)
        self.docker.platform['Architecture'] = {'amd64': 'x86_64', 'arm64': 'aarch64'}[self.architecture]
        patch.object(bundle, 'command', self.docker).start()
        patch('sys.stdout', io.StringIO()).start()

    def export(self):
        bundle.export_bundle(self.export_args)
        return bundle.read_json(self.output / 'manifest.json')

    def write_manifest(self, manifest):
        (self.output / 'manifest.json').write_text(json.dumps(manifest))

    def test_export_import_check_round_trip_and_no_secrets(self):
        manifest = self.export()
        self.assertEqual({p.name for p in self.output.iterdir()}, {'images.tar', 'manifest.json'})
        self.assertNotIn(b'do-not-copy', (self.output / 'images.tar').read_bytes())
        self.assertEqual(len(manifest['images']), 6)
        self.assertFalse(any(call[0] == 'pull' for call in self.docker.calls))
        self.docker.images.clear()  # Destination starts with no local images.
        bundle.use_bundle(self.import_args)
        content = self.env.read_text()
        self.assertIn('# Preserve this comment\nRUN_UID=1000\nDB_SECRET=do-not-copy\n', content)
        for key, item in manifest['images'].items():
            self.assertIn(bundle.ENV[key] + '=' + item['alias'] + '\n', content)
            self.assertNotIn(item['reference'], content)
        backups = list(self.runtime.glob('compose.env.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.import_args.action = 'check'
        bundle.use_bundle(self.import_args)
        self.import_args.action = 'import'
        bundle.use_bundle(self.import_args)
        self.assertEqual(len(list(self.runtime.glob('compose.env.backup-*'))), 1)

    def test_neutral_export_and_legacy_alias_import_keep_source_binding(self):
        manifest = self.export()
        for key, item in manifest['images'].items():
            self.assertTrue(item['alias'].startswith('offline/tailnet-'))
            item['alias'] = item['alias'].replace('offline/tailnet-', 'offline/feishu-')
        self.write_manifest(manifest)
        self.docker.images.clear()
        bundle.use_bundle(self.import_args)
        self.import_args.action = 'check'
        bundle.use_bundle(self.import_args)
        self.assertIn('HEADSCALE_IMAGE=offline/feishu-headscale:', self.env.read_text())
        manifest['integration_commit'] = 'f' * 40
        self.write_manifest(manifest)
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, 'source binding mismatch'):
            bundle.use_bundle(self.import_args)
        self.assertEqual(self.docker.calls, [])

    def test_podman_export_uses_docker_multi_image_archive_and_normalizes_ids(self):
        self.export_args.engine = 'podman'
        self.export_args.pull_supporting_images = True
        manifest = self.export()
        self.assertEqual(manifest['exporter'], 'podman')
        save = next(call for call in self.docker.calls if call[:2] == ('image','save'))
        self.assertIn('--multi-image-archive', save)
        self.assertEqual(save[save.index('--format')+1], 'docker-archive')
        pulls = [call[-1] for call in self.docker.calls if call[0] == 'pull']
        self.assertEqual(pulls, ['docker.io/library/' + self.config['images'][key]
                                 for key in ('database', 'reverse_proxy')])
        self.assertTrue(all(image['id'].startswith('sha256:') for image in manifest['images'].values()))
        self.assertTrue(all(engine == 'podman' for engine in self.docker.engines))
        self.docker.engines.clear()
        self.docker.images.clear()
        bundle.use_bundle(self.import_args)
        self.assertTrue(all(engine == 'docker' for engine in self.docker.engines))

    def test_pull_only_supporting_images_with_platform(self):
        self.export_args.pull_supporting_images = True
        self.export()
        pulls = [call for call in self.docker.calls if call[0] == 'pull']
        self.assertEqual(pulls, [('pull', '--platform', self.config['platform'], self.config['images'][key])
                                 for key in ('database', 'reverse_proxy')])

    def test_export_refuses_dirty_checkout_and_existing_output(self):
        (self.root / 'untracked.txt').write_text('dirty')
        with self.assertRaisesRegex(ValueError, 'clean'):
            self.export()
        self.assertEqual(self.docker.calls, [])
        (self.root / 'untracked.txt').unlink()
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, 'exists'):
            self.export()

    def test_export_rejects_missing_wrong_platform_or_revision_images(self):
        reference = self.headscale_reference
        image = copy.deepcopy(self.docker.images[reference])
        for mutation in ('missing', 'platform', 'revision'):
            with self.subTest(mutation=mutation):
                self.docker.images[reference] = copy.deepcopy(image)
                if mutation == 'missing':
                    del self.docker.images[reference]
                elif mutation == 'platform':
                    self.docker.images[reference]['Architecture'] = self.foreign_architecture
                else:
                    self.docker.images[reference]['Config']['Labels'] = {}
                with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.export()
                self.assertFalse(self.output.exists())

    def test_failed_save_is_not_published(self):
        self.docker.fail_save = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.export()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob('.image-bundle-*')), [])

    def test_checksum_and_source_mismatch_fail_before_docker(self):
        original = self.export()
        for field in ('integration_commit', 'versions_lock_sha256', 'image_inputs_sha256', 'archive'):
            with self.subTest(field=field):
                manifest = copy.deepcopy(original)
                if field == 'archive':
                    manifest[field]['sha256'] = '0' * 64
                else:
                    manifest[field] = '0' * len(manifest[field])
                self.write_manifest(manifest)
                self.docker.calls.clear()
                with self.assertRaisesRegex(ValueError, 'mismatch'):
                    bundle.use_bundle(self.import_args)
                self.assertEqual(self.docker.calls, [])
                self.assertEqual(self.env.read_bytes(), self.original)

    def test_foreign_daemon_fails_before_load(self):
        self.export()
        for platform in ({'OSType': 'windows', 'Architecture': self.architecture},
                         {'OSType': 'linux', 'Architecture': self.foreign_architecture}):
            self.docker.platform = platform
            self.docker.calls.clear()
            with self.assertRaisesRegex(ValueError, 'daemon'):
                bundle.use_bundle(self.import_args)
            self.assertFalse(any(call[:2] == ('image', 'load') for call in self.docker.calls))

    def test_load_validation_failure_preserves_environment(self):
        self.export()
        self.docker.bad_load = True
        with self.assertRaisesRegex(ValueError, 'revision mismatch'):
            bundle.use_bundle(self.import_args)
        self.assertEqual(self.env.read_bytes(), self.original)
        self.assertFalse(any(call[:2] == ('image', 'tag') for call in self.docker.calls))
        self.assertEqual(list(self.runtime.glob('compose.env.backup-*')), [])

    def test_check_rejects_alias_retag_and_changed_pins(self):
        manifest = self.export()
        bundle.use_bundle(self.import_args)
        self.import_args.action = 'check'
        alias = manifest['images']['headscale']['alias']
        original = self.docker.images[alias]
        self.docker.images[alias] = self.docker.images[manifest['images']['headplane']['alias']]
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            bundle.use_bundle(self.import_args)
        self.docker.images[alias] = original
        self.env.write_text(self.env.read_text().replace(alias, 'mutable:latest'))
        with self.assertRaisesRegex(ValueError, 'pins'):
            bundle.use_bundle(self.import_args)

    def test_malformed_manifest_fails_before_docker(self):
        original = self.export()
        for mutation in ('keys', 'id', 'alias', 'platform', 'source', 'archive_path'):
            with self.subTest(mutation=mutation):
                manifest = copy.deepcopy(original)
                item = manifest['images']['headscale']
                if mutation == 'keys':
                    del manifest['images']['database']
                elif mutation == 'id':
                    item['id'] = '--bad'
                elif mutation == 'alias':
                    item['alias'] = 'mutable:latest'
                elif mutation == 'platform':
                    item['architecture'] = self.foreign_architecture
                elif mutation == 'source':
                    item['source_commit'] = 'bad'
                else:
                    manifest['archive']['file'] = '../other.tar'
                self.write_manifest(manifest)
                self.docker.calls.clear()
                with self.assertRaises(ValueError):
                    bundle.use_bundle(self.import_args)
                self.assertEqual(self.docker.calls, [])

    def test_invalid_inputs_and_duplicate_json_are_rejected(self):
        path = self.root / 'image-inputs.json'
        for content in ('[]', '{"schema_version":1,"schema_version":1}',
                        '{"schema_version":1,"platform":"windows/amd64","images":{}}',
                        '{"schema_version":1,"platform":"linux/amd64","images":{"sync":"-bad"}}'):
            path.write_text(content)
            with self.assertRaises(ValueError):
                bundle.inputs()

    def external(self, owner='external'):
        selection = bundle.load_deployment()
        from deployment import make_deployment
        selection = make_deployment('external', 'https://login.example.com', owner)
        descriptor = self.runtime / 'deployment.json'
        descriptor.write_text(json.dumps(selection))
        self.export_args.deployment = descriptor
        return selection

    def test_external_bundle_roundtrip_selects_only_owned_images(self):
        selection = self.external()
        self.export_args.pull_supporting_images = True
        # No local identity or worker images are needed.
        for key in ('casdoor', 'database', 'sync'):
            image_id = 'sha256:' + str(list(bundle.ENV).index(key) + 1) * 64
            self.docker.images = {ref: image for ref, image in self.docker.images.items()
                                  if image['Id'] != image_id}
        manifest = self.export()
        self.assertEqual(manifest['schema_version'], 2)
        self.assertEqual(set(manifest['images']), set(selection['artifacts']))
        self.assertEqual(manifest['deployment_sha256'], bundle.deployment_digest(selection))
        self.assertEqual([call[-1] for call in self.docker.calls if call[0] == 'pull'],
                         [self.config['images']['reverse_proxy']])
        self.docker.images.clear()
        bundle.use_bundle(self.import_args)
        self.import_args.action = 'check'
        bundle.use_bundle(self.import_args)
        for variable in ('CASDOOR_IMAGE', 'POSTGRES_IMAGE', 'WORKER_IMAGE'):
            self.assertNotIn(variable, self.env.read_text())

    def test_external_local_worker_is_included(self):
        selection = self.external('local')
        manifest = self.export()
        self.assertEqual(set(manifest['images']), set(selection['artifacts']))
        self.assertIn('sync', manifest['images'])
        bundle.use_bundle(self.import_args)

    def test_external_bundle_rejects_ownership_and_source_substitution_before_docker(self):
        self.external()
        original = self.export()
        for mutation in ('extra', 'missing', 'revision', 'reference', 'descriptor', 'digest'):
            with self.subTest(mutation=mutation):
                manifest = copy.deepcopy(original)
                if mutation == 'extra':
                    manifest['images']['casdoor'] = manifest['images']['headscale']
                elif mutation == 'missing':
                    del manifest['images']['headplane']
                elif mutation == 'revision':
                    manifest['images']['headscale'].update(revision='f'*40, source_commit='f'*40)
                elif mutation == 'reference':
                    manifest['images']['headscale']['reference'] = 'wrong:latest'
                elif mutation == 'descriptor':
                    manifest['deployment']['identity']['issuer'] = 'https://other.example.com'
                else:
                    manifest['deployment_sha256'] = 'f'*64
                self.write_manifest(manifest)
                self.docker.calls.clear()
                with self.assertRaises(ValueError):
                    bundle.use_bundle(self.import_args)
                self.assertEqual(self.docker.calls, [])
                self.assertEqual(self.env.read_bytes(), self.original)

    def test_legacy_bundle_cannot_be_loaded_into_external_runtime(self):
        self.export()
        self.external()
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, 'Legacy bundle'):
            bundle.use_bundle(self.import_args)
        self.assertEqual(self.docker.calls, [])

    def test_legacy_bundle_supports_new_bundled_runtime_with_complete_selection(self):
        self.export()
        from deployment import make_deployment
        descriptor = make_deployment('bundled', 'https://login.example.com')
        (self.runtime / 'deployment.json').write_text(json.dumps(descriptor))
        bundle.use_bundle(self.import_args)
        self.import_args.action = 'check'
        bundle.use_bundle(self.import_args)

    def test_external_bundle_requires_descriptor_and_unchanged_configuration(self):
        selection = self.external()
        compose = self.runtime / 'compose.yaml'
        compose.write_text('services: {}')
        selection['configuration_sha256'] = {'compose.yaml': bundle.digest(compose)}
        self.export_args.deployment.write_text(json.dumps(selection))
        self.export()
        self.docker.calls.clear()
        with self.assertRaisesRegex(ValueError, 'requires a deployment'):
            bundle.validate_bundle(self.output)
        compose.write_text('services: changed')
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            bundle.use_bundle(self.import_args)
        self.assertEqual(self.docker.calls, [])

    def test_import_and_check_do_not_import_yaml(self):
        self.export()
        import builtins
        original_import = builtins.__import__
        def no_yaml(name, *args, **kwargs):
            if name == 'yaml':
                raise AssertionError('Offline path must use only stdlib')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', no_yaml):
            bundle.use_bundle(self.import_args)
            self.import_args.action = 'check'
            bundle.use_bundle(self.import_args)


if __name__ == '__main__':
    unittest.main()
