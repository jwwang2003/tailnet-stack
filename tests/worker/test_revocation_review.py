"""Adversarial review scenarios for the revocation journal, marker and lock contract.

Written against the recovery contract in sync/README.md and docs/operations.md
after test_revocation_durability.py and test_revocation_journal.py: sequences
those files do not exercise (lock refusal at watch start-up, journal I/O errors
in --offboard, a stale revoked marker after a journal restore, partial failure
across several targets, replay of a journal entry that survived a completed
revocation).
"""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import test_worker as fixtures

w = fixtures.w
ISSUER = "https://login.example.com"
ALICE_NODES, BOB_NODES = ("11", "12"), ("14",)


class RevocationReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkerLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config, self.api = fixture.config, fixture.api
        self.run_worker = fixture.run_worker
        self.state = Path(self.config["state_file"])
        self.journal_path = Path(self.config["state_file"] + ".revocations.json")
        self.heartbeat_path = Path(self.config["state_file"] + ".heartbeat.json")

    # --- helpers ---------------------------------------------------------

    @property
    def alice(self):
        return self.api.users[0]

    @property
    def bob(self):
        return next(user for user in self.api.users if user["lark"] == "ou_bob")

    def link_headscale(self):
        self.api.headscale_users = [{"id": "7", "name": "alice", "providerId": ISSUER + "/casdoor-alice"},
                                    {"id": "8", "name": "other", "providerId": ISSUER + "/someone-else"},
                                    {"id": "9", "name": "bob", "providerId": ISSUER + "/casdoor-bob"}]
        self.api.headscale_nodes = [{"id": "11", "name": "laptop", "user": {"id": "7"}}, {"id": "12", "name": "phone", "user": {"id": "7"}},
                                    {"id": "13", "name": "server", "user": {"id": "8"}}, {"id": "14", "name": "tablet", "user": {"id": "9"}}]
        self.api.headscale_keys = [{"id": "3", "user": {"id": "7"}, "key": "redacted"}, {"id": "4", "user": {"id": "8"}, "key": "redacted"},
                                   {"id": "5", "user": {"id": "9"}, "key": "redacted"}]

    def set_frozen(self, frozen, *open_ids):
        for users in self.api.source_users.values():
            for user in users:
                if user["open_id"] in open_ids:
                    user["status"]["is_frozen"] = frozen

    def node(self, node_id):
        return next(item for item in self.api.headscale_nodes if item["id"] == node_id)

    def key(self, key_id):
        return next(item for item in self.api.headscale_keys if item["id"] == key_id)

    def reenroll(self, *node_ids, key_id=None):
        for node_id in node_ids:
            self.node(node_id).pop("expired", None)
        if key_id:
            self.key(key_id).pop("expired", None)

    def pending(self):
        return sorted(json.loads(self.journal_path.read_text())["pending"]) if self.journal_path.exists() else []

    def expire_node_calls(self):
        return sorted(path.split("/")[-2] for method, path, _, _ in self.api.headscale_calls if method == "POST" and path.endswith("/expire") and "/node/" in path)

    def write_config(self):
        path = self.state.parent / "config.json"
        path.write_text(json.dumps(self.config))
        return path

    def run_main(self, *args, sleep=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(w, "Http", lambda *a, **k: self.api))
            if sleep is not None:
                stack.enter_context(patch.object(w.time, "sleep", sleep))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = w.main(["--config", str(self.write_config()), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def hold(self, suffix):
        path = Path(self.config["state_file"] + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def manual_reenable_leaving_revoked_marker(self):
        """An operator re-enables by hand with allow_reenable off: only the block marker is cleared (documented)."""
        self.alice["isForbidden"] = False
        self.alice["properties"][w.FORBIDDEN_MARKER] = ""
        self.set_frozen(False, "ou_alice")
        self.run_worker()
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        stale = self.alice["properties"][w.REVOKED_MARKER]
        self.assertTrue(stale.isdigit(), "the revoked marker of the previous cycle is left in place by a manual re-enable")
        return stale

    # --- lock interplay --------------------------------------------------

    def test_watch_worker_started_while_the_state_lock_is_held_retries_on_the_next_interval(self):
        """A `--watch` worker must not exit when an --offboard holds the state lock at start-up; the interval is refused and retried."""
        self.link_headscale()
        holder = self.hold(".lock")
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                fcntl.flock(holder, fcntl.LOCK_UN)  # the offboarding finished while the worker waited for its next interval
                return
            raise KeyboardInterrupt

        code, stdout, stderr = self.run_main("--apply", "--watch", sleep=sleep)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(sleeps, [self.config["interval_seconds"]] * 2, "the refused interval sleeps and retries instead of exiting")
        self.assertIn("state lock", stderr, "the refused interval is reported as an error line")
        self.assertIn('"mode": "apply"', stdout, "the next interval runs once the lock is free")
        self.assertTrue(self.heartbeat_path.exists(), "the successful retry writes the heartbeat")

    def test_one_shot_apply_runs_while_a_watch_worker_sleeps(self):
        """The watch lock alone does not exclude a one-shot apply; only the state lock does."""
        self.link_headscale()
        self.hold(".watch.lock")
        code, stdout, stderr = self.run_main("--apply")
        self.assertEqual(code, 0, stderr)
        self.assertIn('"mode": "apply"', stdout)
        self.assertTrue(self.state.exists())

    def test_watch_interval_is_refused_while_a_one_shot_apply_holds_the_state_lock(self):
        """The sleeping watch worker's next interval is refused (not crashed) while another apply holds the lock, then retries."""
        self.link_headscale()
        self.run_worker()
        holder = None
        sleeps = []

        def sleep(seconds):
            nonlocal holder
            sleeps.append(seconds)
            if len(sleeps) == 1:
                holder = self.hold(".lock")  # a one-shot --apply starts while the watch worker sleeps
            elif len(sleeps) == 2:
                fcntl.flock(holder, fcntl.LOCK_UN)
            else:
                raise KeyboardInterrupt

        code, stdout, stderr = self.run_main("--apply", "--watch", sleep=sleep)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(stdout.count('"mode": "apply"'), 2, "intervals 1 and 3 run; interval 2 is refused")
        self.assertEqual(stderr.count("state lock"), 1, stderr)

    # --- --offboard error handling ----------------------------------------

    def test_offboard_reports_a_json_error_when_the_journal_cannot_be_written(self):
        """Journal I/O failure in --offboard --apply must produce the JSON error line and leave the account untouched, not a traceback."""
        self.link_headscale()
        self.run_worker()
        self.api.writes.clear()
        original = w.save_state

        def save_state(path, data):
            if str(path).endswith(".revocations.json"):
                raise OSError(28, "No space left on device")
            return original(path, data)

        with patch.object(w, "save_state", save_state):
            code, stdout, stderr = self.run_main("--offboard", "employees/alice", "--apply")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        error = json.loads(stderr.strip().splitlines()[-1])
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["error"], "local state I/O failed")
        self.assertEqual(self.api.writes, [], "the block is not sent when the intent cannot be journaled")
        self.assertFalse(self.alice["isForbidden"])
        self.assertFalse(self.journal_path.exists())
        handle = self.hold(".lock")
        fcntl.flock(handle, fcntl.LOCK_UN)

    def test_offboard_of_a_missing_account_journals_nothing_and_releases_the_lock(self):
        with self.assertRaisesRegex(w.SyncError, "not found"):
            w.offboard(self.config, "employees/nobody", True, self.api)
        self.assertFalse(self.journal_path.exists())
        self.assertEqual(self.api.writes, [])
        handle = self.hold(".lock")
        fcntl.flock(handle, fcntl.LOCK_UN)

    # --- marker durability -------------------------------------------------

    def test_block_after_manual_reenable_survives_a_journal_restore(self):
        """Double fault: a manual re-enable leaves feishu_sync_revoked in place; if the journal is later restored
        from a backup taken before the next block, the marker-based retry must still see that block."""
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.run_worker()
        self.manual_reenable_leaving_revoked_marker()
        self.reenroll(*ALICE_NODES, key_id="3")
        # Alice leaves again. The block is committed but its response is lost, so the run fails before revocation.
        self.set_frozen(True, "ou_alice")
        self.api.fail_after_update_user_for = {"employees/alice"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.assertFalse(self.alice["properties"].get(w.REVOKED_MARKER), "a new block must not carry the previous cycle's revoked marker")
        # The state directory is restored from a backup taken before that run: the journal entry is gone.
        self.journal_path.unlink()
        self.api.fail_after_update_user_for = set()
        self.api.headscale_calls.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["targets"], 1, "the forbidden user with a worker marker is still a target")
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(self.expire_node_calls(), ["11", "12"])
        self.assertTrue(self.key("3").get("expired"))
        self.assertTrue(self.alice["properties"][w.REVOKED_MARKER].isdigit())

    def test_offboard_after_manual_reenable_survives_a_journal_restore(self):
        """Same double fault through --offboard: the hold must not be hidden behind a stale revoked marker."""
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.run_worker()
        self.manual_reenable_leaving_revoked_marker()
        self.reenroll(*ALICE_NODES, key_id="3")
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaisesRegex(w.SyncError, "revocation failed"):
            w.offboard(self.config, "employees/alice", True, self.api)
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.alice["properties"][w.HOLD_MARKER], "true")
        self.assertFalse(self.alice["properties"].get(w.REVOKED_MARKER), "an offboarding block must not carry a stale revoked marker")
        self.journal_path.unlink()
        self.api.headscale_fail_paths = set()
        self.api.headscale_calls.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(self.expire_node_calls(), ["11", "12"])
        self.assertTrue(self.alice["properties"][w.REVOKED_MARKER].isdigit())

    def test_journal_entry_surviving_a_completed_revocation_is_replayed_and_dropped(self):
        """Killed after the marker write but before the journal drop: the replay is idempotent and clears the entry without an error."""
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.run_worker()
        first_marker = self.alice["properties"][w.REVOKED_MARKER]
        self.journal_path.write_text(json.dumps({"version": 1, "pending": {"casdoor-alice": {"since": int(time.time()) - 5, "source": "sync"}}}))
        self.api.headscale_calls.clear()
        self.api.writes.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0, "headscale_users": 1,
                                                "nodes_expired": 2, "nodes_deleted": 0, "preauth_keys_expired": 1})
        self.assertEqual(self.expire_node_calls(), ["11", "12"], "re-expiring already expired nodes is the idempotent replay")
        self.assertEqual(self.pending(), [])
        self.assertGreaterEqual(int(self.alice["properties"][w.REVOKED_MARKER]), int(first_marker))
        self.assertEqual(self.api.writes.count("update-user"), 1, "only the marker is rewritten")

    def test_journal_entry_outranks_a_manually_removed_worker_marker(self):
        """Documented precedence: a journaled block is revoked even after an operator strips feishu_sync_forbidden.

        The journal is the worker's own durable record that it applied this block; removing the marker while
        leaving isForbidden set does not turn it into an administrator block. Only a user forbidden with
        neither marker *and* no journal entry is left alone (test_operator_block_without_markers_is_never_revoked).
        """
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.alice["properties"][w.FORBIDDEN_MARKER] = ""
        self.api.headscale_fail_paths = set()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertTrue(self.node("11").get("expired") and self.node("12").get("expired"))
        self.assertEqual(self.pending(), [])
        self.assertTrue(self.alice["isForbidden"])

    # --- several targets, partial failure ----------------------------------

    def test_one_failing_target_does_not_block_the_others(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice", "ou_bob")
        self.api.headscale_fail_paths = {"/node/11/expire"}
        with self.assertRaisesRegex(w.SyncError, r"pending for 1 subject.*fixture Headscale failure on /api/v1/node/11/expire"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"] and self.bob["isForbidden"])
        self.assertTrue(self.node("14").get("expired") and self.key("5").get("expired"), "bob is revoked although alice failed first")
        self.assertTrue(self.bob["properties"][w.REVOKED_MARKER].isdigit())
        self.assertFalse(self.node("11").get("expired"))
        self.assertFalse(self.alice["properties"].get(w.REVOKED_MARKER))
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.assertEqual(json.loads(self.state.read_text())["known_users"], {"ou_alice": "casdoor-alice", "ou_bob": "casdoor-bob"})
        self.api.headscale_fail_paths = set()
        self.api.headscale_calls.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0, "headscale_users": 1,
                                                "nodes_expired": 2, "nodes_deleted": 0, "preauth_keys_expired": 1})
        self.assertEqual(self.expire_node_calls(), ["11", "12"], "bob is not touched again")
        self.assertEqual(self.pending(), [])

    # --- report semantics ----------------------------------------------------

    def test_pending_revocation_run_writes_state_but_no_heartbeat(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.headscale_fail_paths = {"/api/v1/node"}
        code, stdout, stderr = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("revocation is pending", stderr)
        self.assertFalse(self.heartbeat_path.exists(), "a pending revocation is not a successful interval")
        self.assertEqual(self.pending(), ["casdoor-alice"])
        self.api.headscale_fail_paths = set()
        code, stdout, _ = self.run_main("--apply")
        self.assertEqual(code, 0)
        self.assertTrue(self.heartbeat_path.exists())
        self.assertEqual(self.pending(), [])

    def test_dry_run_counts_legacy_pending_revocations_without_migrating_them(self):
        self.link_headscale()
        self.run_worker()
        self.alice.update(isForbidden=True, groups=["employees/local-operators"])
        self.alice["properties"].update({w.FORBIDDEN_MARKER: "inactive", "headplane_role": "member"})
        self.set_frozen(True, "ou_alice")
        legacy = json.loads(self.state.read_text())
        legacy["pending_revocations"] = ["casdoor-alice"]
        self.state.write_text(json.dumps(legacy))
        self.api.writes.clear()
        report = self.run_worker(False)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["revocation"]["targets"], 1)
        self.assertEqual(report["revocation"]["pending"], 1)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.journal_path.exists(), "a dry run never writes the journal")
        self.assertEqual(json.loads(self.state.read_text())["pending_revocations"], ["casdoor-alice"], "a dry run never rewrites state")


if __name__ == "__main__":
    unittest.main()
