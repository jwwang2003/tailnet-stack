import copy
import hashlib
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        self.lock = release.read_yaml(ROOT / "versions.lock.yaml")

    def test_candidate_is_not_promotable(self):
        self.lock["release"]["compatibility_verified"] = False
        with self.assertRaisesRegex(ValueError, "compatibility"):
            release.check_promotion(self.lock, {}, b"candidate")

    def test_main_and_dirty_sources_refused(self):
        with patch.object(release, "git", return_value="main"):
            with self.assertRaisesRegex(ValueError, "downstream branch"):
                release.verify_sources(self.lock, "/unused")
        with patch.object(release, "git", side_effect=["feature/test", " M unsafe.py"]):
            with self.assertRaisesRegex(ValueError, "uncommitted"):
                release.verify_sources(self.lock, "/unused")

    def test_source_commit_mismatch_refused(self):
        with patch.object(release, "git", side_effect=["feature/test", "", "f" * 40]):
            with self.assertRaisesRegex(ValueError, "HEAD"):
                release.verify_sources(self.lock, "/unused")

    def test_production_requires_recorded_evidence(self):
        lock = copy.deepcopy(self.lock)
        lock["release"]["compatibility_verified"] = True
        digest = "sha256:" + "a" * 64
        images = {}
        for component in release.COMPONENTS:
            entry = lock["components"][component]
            entry["image_digest"] = digest
            images[component] = entry["image"] + "@" + digest
        images.update({key: "example.invalid/image@" + digest for key in ("sync", "reverse_proxy", "database")})
        manifest = {
            "release": "test", "integration_commit": "b" * 40,
            "versions_lock_sha256": hashlib.sha256(b"lock").hexdigest(),
            "created_at_utc": "2026-09-14T00:00:00Z", "platform": "linux/amd64",
            "images": images, "configuration_sha256": {"config": "c" * 64},
            "toolchains": {"go": "1.26.5"},
            "migrations": {component: "no migration" for component in release.COMPONENTS},
            "validation": {field: {"passed": True, "evidence": "recorded test report"} for field in (
                "oauth_and_identity_linking", "directory_lifecycle", "headscale_and_headplane_oidc",
                "existing_access_revocation", "isolated_restore")},
            "backup": {"reference": "encrypted-backup", "restored_successfully_at_utc": "2026-09-14T00:00:00Z"},
        }
        release.check_promotion(lock, manifest, b"lock")
        manifest["validation"]["isolated_restore"]["passed"] = False
        with self.assertRaisesRegex(ValueError, "isolated_restore"):
            release.check_promotion(lock, manifest, b"lock")
        manifest["validation"]["isolated_restore"]["passed"] = True
        manifest["images"]["casdoor"] = "casdoor:latest"
        with self.assertRaisesRegex(ValueError, "differs"):
            release.check_promotion(lock, manifest, b"lock")

class ExternalReleaseChecks(unittest.TestCase):
    def setUp(self):
        from deployment import make_deployment
        self.deployment = make_deployment('external', 'https://login.example.com')
        self.lock = release.read_yaml(ROOT / 'versions.lock.yaml')
        self.lock['components'].pop('casdoor')
        self.lock['release']['compatibility_verified'] = True
        images = {}
        for key, entry in self.lock['components'].items():
            entry['image_digest'] = 'sha256:' + 'a'*64
            images[key] = entry['image'] + '@' + entry['image_digest']
        images['reverse_proxy'] = 'caddy@sha256:' + 'b'*64
        self.manifest = {
            'deployment': self.deployment,
            'deployment_sha256': release.deployment_digest(self.deployment),
            'release': 'external-test', 'integration_commit': 'b'*40,
            'versions_lock_sha256': hashlib.sha256(b'lock').hexdigest(),
            'created_at_utc': '2026-09-15T00:00:00Z', 'platform': 'linux/amd64',
            'images': images, 'configuration_sha256': {'compose.yaml': 'c'*64},
            'toolchains': {'python': '3.12'},
            'migrations': {'headscale': 'none', 'headplane': 'none'},
            'validation': {field: {'passed': True, 'evidence': 'test report'} for field in (
                'oauth_and_identity_linking', 'directory_lifecycle', 'headscale_and_headplane_oidc',
                'existing_access_revocation', 'isolated_restore')},
            'backup': {'reference': 'tailnet-backup', 'restored_successfully_at_utc': '2026-09-15T00:00:00Z'},
            'external_identity': {'issuer': 'https://login.example.com', 'version': 'Casdoor test',
                'evidence': 'external login acceptance', 'recovery_reference': 'identity-owner runbook'},
            'not_applicable': {'casdoor_migration': 'Managed by identity owner',
                'casdoor_database_backup': 'Managed by identity owner'},
        }

    def check(self):
        release.check_promotion(self.lock, self.manifest, b'lock', deployment=self.deployment)

    def test_external_selection_does_not_require_casdoor_checkout_or_catalog_entry(self):
        release.check_lock(self.lock, release.selected_products(self.deployment))
        values = []
        for key in ('headscale', 'headplane'):
            values.extend(['downstream/test', '', self.lock['components'][key]['source_commit']])
        with patch.object(release, 'git', side_effect=values) as git:
            release.verify_sources(self.lock, '/unused', self.deployment)
            self.assertEqual(git.call_count, 6)
        with self.assertRaises(ValueError):
            release.check_lock(self.lock)
        self.check()

    def test_external_evidence_and_tailnet_recovery_are_mandatory(self):
        for field in ('issuer', 'version', 'evidence', 'recovery_reference'):
            with self.subTest(field=field):
                original = self.manifest['external_identity'].pop(field)
                with self.assertRaisesRegex(ValueError, 'External issuer'):
                    self.check()
                self.manifest['external_identity'][field] = original
        for field in ('headscale_and_headplane_oidc', 'existing_access_revocation', 'isolated_restore'):
            self.manifest['validation'][field]['passed'] = False
            with self.assertRaisesRegex(ValueError, field):
                self.check()
            self.manifest['validation'][field]['passed'] = True
        self.manifest['backup'] = {}
        with self.assertRaisesRegex(ValueError, 'Backup/restore'):
            self.check()

    def test_external_offline_promotion_binds_selected_sources_and_configuration(self):
        bundle = {'schema_version': 2, 'deployment': self.deployment,
            'deployment_sha256': release.deployment_digest(self.deployment),
            'integration_commit': self.manifest['integration_commit'],
            'versions_lock_sha256': self.manifest['versions_lock_sha256'],
            'platform': self.manifest['platform'],
            'archive': {'file': 'images.tar', 'sha256': 'd'*64}, 'images': {}}
        self.manifest.update(distribution='offline', bundle_sha256='d'*64)
        for key in self.deployment['artifacts']:
            entry = self.lock['components'].get(key, {})
            alias = 'offline/tailnet-' + key.replace('_', '-') + ':sha256-' + 'a'*64
            bundle['images'][key] = {'alias': alias, 'id': 'sha256:' + 'a'*64,
                'os': 'linux', 'architecture': 'amd64', 'revision': entry.get('source_commit'),
                'source_commit': entry.get('source_commit'), 'reference': entry.get('image', 'caddy:test')}
            self.manifest['images'][key] = alias
        release.check_promotion(self.lock, self.manifest, b'lock', bundle, self.deployment)
        bundle['images']['headscale']['source_commit'] = 'e'*40
        with self.assertRaisesRegex(ValueError, 'source lock'):
            release.check_promotion(self.lock, self.manifest, b'lock', bundle, self.deployment)
        self.deployment['configuration_sha256'] = {'compose.yaml': 'f'*64}
        self.manifest['deployment_sha256'] = release.deployment_digest(self.deployment)
        with self.assertRaisesRegex(ValueError, 'configuration differs'):
            self.check()

    def test_extra_images_and_altered_binding_fail(self):
        self.manifest['images']['casdoor'] = 'casdoor:latest'
        with self.assertRaisesRegex(ValueError, 'selection'):
            self.check()
        del self.manifest['images']['casdoor']
        self.manifest['deployment_sha256'] = 'f'*64
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.check()


class OfflineDistributionChecks(unittest.TestCase):
    def setUp(self):
        self.lock = release.read_yaml(ROOT / 'versions.lock.yaml')
        self.bundle = {'schema_version':1, 'integration_commit':'a'*40,
            'versions_lock_sha256':hashlib.sha256(b'lock').hexdigest(),
            'platform':'linux/amd64', 'archive':{'file':'images.tar','sha256':'b'*64},'images':{}}
        self.manifest = {'integration_commit':'a'*40,'platform':'linux/amd64','bundle_sha256':'b'*64,'images':{}}
        for key in (*release.COMPONENTS,'sync','database','reverse_proxy'):
            alias='offline/feishu-' + key.replace('_', '-') + ':sha256-' + 'c'*64
            rev = self.lock['components'][key]['source_commit'] if key in release.COMPONENTS else 'a'*40 if key=='sync' else None
            self.bundle['images'][key]={'id':'sha256:'+'c'*64,'os':'linux','architecture':'amd64','revision':rev,'alias':alias}
            self.manifest['images'][key]=alias

    def test_no_registry_digests_required_for_verified_offline_images(self):
        release.check_offline_images(self.lock,self.manifest,b'lock',self.bundle)

    def test_neutral_and_legacy_aliases_require_matching_content_ids(self):
        for namespace in ('tailnet', 'feishu'):
            for key, item in self.bundle['images'].items():
                alias = f"offline/{namespace}-{key.replace('_', '-')}:sha256-" + 'c' * 64
                item['alias'] = self.manifest['images'][key] = alias
            release.check_offline_images(self.lock, self.manifest, b'lock', self.bundle)
            item = self.bundle['images']['headscale']
            item['alias'] = item['alias'].replace('c' * 64, 'd' * 64)
            self.manifest['images']['headscale'] = item['alias']
            with self.assertRaisesRegex(ValueError, 'alias differs'):
                release.check_offline_images(self.lock, self.manifest, b'lock', self.bundle)

    def test_altered_bundle_or_missing_image_rejected(self):
        for mutation in ('archive','revision','platform','missing'):
            bundle=copy.deepcopy(self.bundle)
            if mutation=='archive':bundle['archive']['sha256']='d'*64
            if mutation=='revision':bundle['images']['casdoor']['revision']='f'*40
            if mutation=='platform':bundle['images']['sync']['architecture']='arm64'
            if mutation=='missing':bundle['images'].pop('database')
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                release.check_offline_images(self.lock,self.manifest,b'lock',bundle)


if __name__ == "__main__":
    unittest.main()
