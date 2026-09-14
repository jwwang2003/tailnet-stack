import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
