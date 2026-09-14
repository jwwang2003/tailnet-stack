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


if __name__ == "__main__":
    unittest.main()
