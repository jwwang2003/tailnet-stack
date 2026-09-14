"""Revocation journal edge cases: schema, dry runs, unconfigured Headscale, stale markers, CLI locks.

Complements test_revocation_durability.py, which covers the two P1 recovery
sequences; this file covers the journal contract around them.
"""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_worker as fixtures

w = fixtures.w
ISSUER = "https://login.example.com"
ZERO = {"headscale_users": 0, "nodes_expired": 0, "nodes_deleted": 0, "preauth_keys_expired": 0}


class RevocationJournalTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkerLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config, self.api = fixture.config, fixture.api
        self.run_worker = fixture.run_worker
        self.state = Path(self.config["state_file"])
        self.journal_path = Path(self.config["state_file"] + ".revocations.json")

    @property
    def alice(self):
        return self.api.users[0]

    def link_headscale(self):
        self.api.headscale_users = [{"id": "7", "name": "alice", "providerId": ISSUER + "/casdoor-alice"}]
        self.api.headscale_nodes = [{"id": "11", "name": "laptop", "user": {"id": "7"}}]
        self.api.headscale_keys = [{"id": "3", "user": {"id": "7"}, "key": "redacted"}]

    def freeze_alice(self, frozen=True):
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = frozen

    def pending(self):
        return sorted(json.loads(self.journal_path.read_text())["pending"]) if self.journal_path.exists() else []

    def headscale_paths(self):
        return [path for _, path, _, _ in self.api.headscale_calls]

    def write_config(self):
        path = self.state.parent / "config.json"
        path.write_text(json.dumps(self.config))
        return path

    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(w, "Http", lambda *a, **k: self.api), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = w.main(["--config", str(self.write_config()), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def hold(self, suffix):
        path = Path(self.config["state_file"] + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def test_corrupt_journal_stops_every_mode_before_any_write(self):
        self.run_worker()
        corrupt = ("{not json", json.dumps({"version": 2, "pending": {}}), json.dumps({"version": 1, "pending": []}),
                   json.dumps({"version": 1, "pending": {"casdoor-alice": {"since": "now", "source": "sync"}}}),
                   json.dumps({"version": 1, "pending": {"casdoor-alice": {"since": 1, "source": "manual"}}}),
                   json.dumps({"version": 1, "pending": {"employees/alice": {"since": 1, "source": "sync"}}}))
        for text in corrupt:
            self.journal_path.write_text(text)
            self.api.writes.clear()
            for apply in (False, True):
                with self.assertRaisesRegex(w.SyncError, "journal"):
                    self.run_worker(apply)
            with self.assertRaisesRegex(w.SyncError, "journal"):
                w.offboard(self.config, "employees/alice", True, self.api)
            self.assertEqual(self.api.writes, [], text)
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.headscale_paths(), [])

    def test_dry_run_reports_planned_block_without_journal_or_writes(self):
        self.link_headscale()
        self.run_worker()
        self.freeze_alice()
        self.api.writes.clear()
        report = self.run_worker(False)
        self.assertEqual(report["users_blocked"], 1)
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 0, "pending": 1, **ZERO})
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(self.headscale_paths(), [])
        self.assertFalse(self.alice["isForbidden"])

    def test_unconfigured_headscale_keeps_journal_entries_until_configured(self):
        self.link_headscale()
        headscale = self.config["headscale"]
        self.config["headscale"] = None
        self.run_worker()
        self.freeze_alice()
        report = self.run_worker()
        self.assertEqual(report["revocation"], {"configured": False, "targets": 1, "revoked": 0, "pending": 1, **ZERO})
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.assertEqual(self.run_worker()["revocation"]["pending"], 1, "the entry stays pending on every run")
        self.assertEqual(self.headscale_paths(), [])
        self.assertNotIn(w.REVOKED_MARKER, self.alice["properties"])
        self.config["headscale"] = headscale
        report = self.run_worker()
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0,
                                                "headscale_users": 1, "nodes_expired": 1, "nodes_deleted": 0, "preauth_keys_expired": 1})
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.alice["properties"][w.REVOKED_MARKER].isdigit())

    def test_plan_never_rewrites_the_marker_and_the_journal_drives_a_later_block(self):
        self.link_headscale()
        self.run_worker()
        self.freeze_alice()
        self.run_worker()
        marker = self.alice["properties"][w.REVOKED_MARKER]
        self.run_worker()
        self.assertEqual(self.alice["properties"][w.REVOKED_MARKER], marker, "a converged run leaves the marker alone")
        # An operator re-enables by hand (allow_reenable stays false) and clears only the block marker.
        self.alice["isForbidden"] = False
        self.alice["properties"][w.FORBIDDEN_MARKER] = ""
        self.freeze_alice(False)
        self.run_worker()
        self.assertFalse(self.alice["isForbidden"])
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        self.assertEqual(self.alice["properties"][w.REVOKED_MARKER], marker, "plan never overwrites the marker")
        self.api.headscale_nodes[0].pop("expired")
        self.api.headscale_keys[0].pop("expired")
        self.freeze_alice()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertTrue(self.api.headscale_nodes[0]["expired"])
        self.assertGreaterEqual(int(self.alice["properties"][w.REVOKED_MARKER]), int(marker))
        self.assertEqual(self.pending(), [])

    def test_offboard_of_unlinked_local_account_is_journaled_and_completed_by_the_worker(self):
        self.api.users.append({"owner": "employees", "name": "svc", "id": "casdoor-svc", "lark": "", "groups": ["employees/tailnet-members", "employees/ops"], "properties": {}, "isForbidden": False})
        self.api.headscale_users = [{"id": "9", "name": "svc", "providerId": ISSUER + "/casdoor-svc"}]
        self.api.headscale_nodes = [{"id": "21", "name": "box", "user": {"id": "9"}}]
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaisesRegex(w.SyncError, "revocation"):
            w.offboard(self.config, "employees/svc", True, self.api)
        svc = next(user for user in self.api.users if user["name"] == "svc")
        self.assertTrue(svc["isForbidden"])
        self.assertEqual(svc["groups"], ["employees/ops"])
        self.assertEqual(self.pending(), ["casdoor-svc"])
        self.api.headscale_fail_paths = set()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertTrue(self.api.headscale_nodes[0]["expired"])
        self.assertEqual(self.pending(), [])
        self.assertTrue(svc["properties"][w.REVOKED_MARKER].isdigit())
        self.assertFalse(svc.get("lark"), "the account never gains a Feishu binding")

    def test_journal_subject_without_casdoor_account_is_still_revoked_then_dropped(self):
        self.run_worker()
        self.api.headscale_users = [{"id": "9", "name": "gone", "providerId": ISSUER + "/casdoor-gone"}]
        self.api.headscale_nodes = [{"id": "21", "name": "box", "user": {"id": "9"}}]
        self.journal_path.write_text(json.dumps({"version": 1, "pending": {"casdoor-gone": {"since": 1, "source": "offboard"}}}))
        self.api.writes.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertTrue(self.api.headscale_nodes[0]["expired"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.api.writes.count("update-user"), 0, "no Casdoor account exists to carry the marker")

    def test_failed_marker_write_keeps_the_entry_for_the_next_run(self):
        self.link_headscale()
        self.run_worker()
        self.freeze_alice()

        def reject_writes_after_headscale(method, path, query, body):
            # The block itself succeeds; Casdoor rejects the marker write that follows the revocation.
            if path.startswith("/api/v1/"):
                self.api.fail_update_user_for = {"employees/alice"}
        self.api.on_request = reject_writes_after_headscale
        with self.assertRaisesRegex(w.SyncError, "revocation"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.assertTrue(self.api.headscale_nodes[0]["expired"])
        self.assertNotIn(w.REVOKED_MARKER, self.alice["properties"])
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.api.on_request = None
        self.api.fail_update_user_for = set()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.alice["properties"][w.REVOKED_MARKER].isdigit())

    def test_unreadable_headscale_key_leaves_everything_pending(self):
        self.link_headscale()
        self.run_worker()
        self.freeze_alice()
        Path(self.config["headscale"]["api_key_file"]).unlink()
        with self.assertRaisesRegex(w.SyncError, "revocation.*API key"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.assertTrue(self.state.exists())
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.assertEqual(self.headscale_paths(), [])

    def test_one_shot_apply_is_refused_while_the_state_lock_is_held(self):
        self.hold(".lock")
        code, _, stderr = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("state lock", stderr)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.state.exists())

    def test_second_watch_worker_is_refused_while_the_first_sleeps(self):
        self.hold(".watch.lock")
        # The state lock is free while the first worker sleeps; a second continuous worker must still not start.
        code, _, stderr = self.run_main("--apply", "--watch")
        self.assertEqual(code, 1)
        self.assertIn("lock", stderr)
        self.assertEqual(self.api.writes, [])

    def test_offboard_through_the_cli_releases_the_lock(self):
        self.link_headscale()
        self.run_worker()
        code, stdout, stderr = self.run_main("--offboard", "employees/alice", "--apply")
        self.assertEqual(code, 0, stderr)
        self.assertIn('"nodes_expired": 1', stdout)
        for suffix in (".lock", ".watch.lock"):
            handle = self.hold(suffix)
            fcntl.flock(handle, fcntl.LOCK_UN)
        self.assertTrue(self.alice["properties"][w.REVOKED_MARKER].isdigit())

    def test_lock_wait_seconds_is_validated(self):
        for value, valid in ((0, False), (1, True), (3600, True), (3601, False), ("300", False), (True, False)):
            self.config["lock_wait_seconds"] = value
            path = self.write_config()
            if valid:
                self.assertEqual(w.load_config(path)["lock_wait_seconds"], value)
            else:
                with self.assertRaisesRegex(w.SyncError, "lock_wait_seconds"):
                    w.load_config(path)
        del self.config["lock_wait_seconds"]
        self.assertEqual(w.load_config(self.write_config())["lock_wait_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
