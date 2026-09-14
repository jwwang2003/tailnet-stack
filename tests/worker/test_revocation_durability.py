"""Revocation durability: journal, Casdoor marker, offboarding retry and lock serialization.

Regression tests for the two P1 code blockers in docs/production-readiness.md
("Code blockers"): a partial sync must not lose an applied block's Headscale
revocation, and a failed `--offboard` revocation must be retried by the next
scheduled run. The tests target the recovery contract (journal file, Casdoor
`feishu_sync_revoked` marker, report counters, state lock), not rc.2 code paths.
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
# Resolved lazily so the whole module still imports (and fails per test) before the fix lands.
REVOKED_MARKER = getattr(w, "REVOKED_MARKER", "feishu_sync_revoked")
ISSUER = "https://login.example.com"
ALICE_NODES, BOB_NODES = ("11", "12"), ("14",)


class RevocationDurabilityTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkerLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config, self.api = fixture.config, fixture.api
        self.run_worker = fixture.run_worker
        self.state = Path(self.config["state_file"])
        self.journal_path = Path(self.config["state_file"] + ".revocations.json")
        self.lock_path = Path(self.config["state_file"] + ".lock")

    # --- fixture helpers -------------------------------------------------

    @property
    def alice(self):
        return self.api.users[0]

    @property
    def bob(self):
        return next(user for user in self.api.users if user["lark"] == "ou_bob")

    def link_headscale(self):
        """Alice (subject casdoor-alice) and Bob (casdoor-bob) both own enrolled nodes and a preauth key."""
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

    def drop_status(self):
        for users in self.api.source_users.values():
            for user in users:
                user.pop("status", None)

    def journal(self):
        return json.loads(self.journal_path.read_text())

    def pending_subjects(self):
        return sorted(self.journal()["pending"]) if self.journal_path.exists() else []

    def node(self, node_id):
        return next(item for item in self.api.headscale_nodes if item["id"] == node_id)

    def key(self, key_id):
        return next(item for item in self.api.headscale_keys if item["id"] == key_id)

    def expired_nodes(self):
        return sorted(node["id"] for node in self.api.headscale_nodes if node.get("expired"))

    def assert_nodes_expired(self, *node_ids):
        for node_id in node_ids:
            self.assertTrue(self.node(node_id).get("expired"), "Headscale node %s must be expired; expired=%s" % (node_id, self.expired_nodes()))

    def assert_nodes_active(self, *node_ids):
        for node_id in node_ids:
            self.assertFalse(self.node(node_id).get("expired"), "Headscale node %s must still be active" % node_id)

    def expire_node_calls(self):
        return sorted(path.split("/")[-2] for method, path, _, _ in self.api.headscale_calls if method == "POST" and path.endswith("/expire") and "/node/" in path)

    def assert_revoked_marker(self, user):
        value = (user.get("properties") or {}).get(REVOKED_MARKER)
        self.assertIsInstance(value, str, "%s must carry the %s marker after revocation" % (user["name"], REVOKED_MARKER))
        self.assertTrue(value.isdigit() and int(value) <= int(time.time()) + 1, "marker must be a unix timestamp string, got %r" % value)

    def lock_probe(self):
        """'held' when another open file description holds the worker's state lock."""
        with self.lock_path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "held"
            fcntl.flock(handle, fcntl.LOCK_UN)
            return "free"

    def offboard_alice_expecting_failure(self):
        """Contract accepts either a SyncError or a report with pending > 0 after a Headscale failure."""
        try:
            report = w.offboard(self.config, "employees/alice", True, self.api)
        except w.SyncError:
            return None
        self.assertGreater(report["revocation"]["pending"], 0, "offboard must not report success when Headscale revocation failed")
        return report

    # --- P1: partial sync -----------------------------------------------

    def test_p1_partial_sync_does_not_lose_first_users_revocation(self):
        """Blocker 1: the first block is applied, the second Casdoor update fails, the retry must still revoke the first user."""
        self.link_headscale()
        self.run_worker()
        self.assertEqual(self.bob["id"], "casdoor-bob")
        self.set_frozen(True, "ou_alice", "ou_bob")
        self.api.fail_update_user_for = {"employees/bob"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"], "alice's block is applied before bob's update fails")
        self.assertFalse(self.bob["isForbidden"], "bob's update was rejected before the write")
        self.api.fail_update_user_for = set()
        report = self.run_worker()
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertTrue(self.key("3").get("expired"), "alice's preauth key must be expired")
        self.assert_nodes_expired(*BOB_NODES)
        self.assert_nodes_active("13")
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)
        self.assert_revoked_marker(self.bob)

    def test_journal_records_subject_before_each_blocking_update(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice", "ou_bob")
        self.api.fail_update_user_for = {"employees/bob"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertTrue(self.journal_path.exists(), "the journal must be written before the blocking update is sent")
        journal = self.journal()
        self.assertEqual(journal["version"], 1)
        self.assertEqual(sorted(journal["pending"]), ["casdoor-alice", "casdoor-bob"], "bob is journaled before his update failed")
        for entry in journal["pending"].values():
            self.assertEqual(entry["source"], "sync")
            self.assertIsInstance(entry["since"], int)
            self.assertLessEqual(entry["since"], int(time.time()) + 1)

    def test_ambiguous_block_response_is_revoked_on_next_run(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.fail_after_update_user_for = {"employees/alice"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"], "the write reached Casdoor although the response was lost")
        self.api.fail_after_update_user_for = set()
        report = self.run_worker()
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertTrue(self.key("3").get("expired"))
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)

    def test_crash_between_blocks_and_revocation_revokes_both_next_run(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice", "ou_bob")
        self.api.headscale_fail_paths = {"/api/v1/user"}
        with self.assertRaisesRegex(w.SyncError, "revocation"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"] and self.bob["isForbidden"])
        self.assertEqual(self.pending_subjects(), ["casdoor-alice", "casdoor-bob"])
        self.assertTrue(self.state.exists(), "snapshot state is saved before the pending revocation is raised")
        self.api.headscale_fail_paths = set()
        report = self.run_worker()
        self.assert_nodes_expired(*ALICE_NODES, *BOB_NODES)
        self.assert_nodes_active("13")
        self.assertTrue(self.key("3").get("expired") and self.key("5").get("expired"))
        self.assertFalse(self.key("4").get("expired"))
        self.assertEqual(report["revocation"]["revoked"], 2)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(self.journal()["pending"] if self.journal_path.exists() else {}, {})
        self.assert_revoked_marker(self.alice)
        self.assert_revoked_marker(self.bob)

    def test_pending_revocation_saves_state_then_raises(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaisesRegex(w.SyncError, "revocation"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(json.loads(self.state.read_text())["known_users"], {"ou_alice": "casdoor-alice", "ou_bob": "casdoor-bob"})
        self.assertEqual(self.journal()["pending"]["casdoor-alice"]["source"], "sync")
        self.assert_nodes_active(*ALICE_NODES)
        self.assertNotIn(REVOKED_MARKER, {key for key, value in self.alice["properties"].items() if value})

    # --- P1: immediate offboarding ---------------------------------------

    def test_p1_offboard_headscale_failure_is_retried_by_scheduled_run(self):
        """Blocker 2: a failed --offboard revocation is durable and the next scheduled run completes it."""
        self.link_headscale()
        self.run_worker()
        self.api.fail_path = "/api/v1/node"
        self.offboard_alice_expecting_failure()
        self.assertTrue(self.alice["isForbidden"], "the Casdoor block is applied even though Headscale failed")
        self.assertEqual(self.alice["properties"][w.HOLD_MARKER], "true")
        self.assert_nodes_active(*ALICE_NODES)
        self.api.fail_path = None
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply")
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertTrue(self.key("3").get("expired"), "alice's preauth key must be expired by the retry")
        self.assert_nodes_active("13", "14")
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0, "headscale_users": 1,
                                                "nodes_expired": 2, "nodes_deleted": 0, "preauth_keys_expired": 1})
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)
        # The hold still survives later runs, and nothing is revoked twice.
        self.api.headscale_calls.clear()
        self.assertEqual(self.run_worker()["revocation"]["targets"], 0)
        self.assertEqual(self.api.headscale_calls, [])
        self.assertTrue(self.alice["isForbidden"])

    def test_offboard_failure_journals_subject_with_offboard_source(self):
        self.link_headscale()
        self.run_worker()
        self.api.headscale_fail_paths = {"/api/v1/node"}
        self.offboard_alice_expecting_failure()
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.alice["properties"][w.HOLD_MARKER], "true")
        journal = self.journal()
        self.assertEqual(journal["version"], 1)
        self.assertEqual(journal["pending"]["casdoor-alice"]["source"], "offboard")
        self.assertIsInstance(journal["pending"]["casdoor-alice"]["since"], int)
        self.assertEqual(json.loads(self.state.read_text())["known_users"], {"ou_alice": "casdoor-alice", "ou_bob": "casdoor-bob"},
                         "offboarding must not rewrite the scheduled worker's snapshot state")

    def test_offboard_success_sets_marker_and_leaves_no_journal_entry(self):
        self.link_headscale()
        self.run_worker()
        report = w.offboard(self.config, "employees/alice", True, self.api)
        self.assertEqual(report["mode"], "offboard-apply")
        for field, value in (("nodes_expired", 2), ("preauth_keys_expired", 1), ("headscale_users", 1), ("nodes_deleted", 0)):
            self.assertEqual(report["revocation"][field], value)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)
        self.api.headscale_calls.clear()
        self.assertEqual(self.run_worker()["revocation"]["targets"], 0)
        self.assertEqual(self.api.headscale_calls, [])

    # --- marker, self-healing and safety ---------------------------------

    def test_revoked_marker_constant(self):
        self.assertEqual(w.REVOKED_MARKER, "feishu_sync_revoked")

    def test_successful_block_reports_counters_and_marks_user_after_headscale(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        report = self.run_worker()
        self.assertEqual(report["users_blocked"], 1)
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0, "headscale_users": 1,
                                                "nodes_expired": 2, "nodes_deleted": 0, "preauth_keys_expired": 1})
        self.assertEqual(self.expire_node_calls(), ["11", "12"])
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)
        marker_write = ("POST", "/api/update-user", {"id": "employees/alice", "columns": "properties"})
        self.assertIn(marker_write, self.api.calls, "the marker is written through update-user?columns=properties")
        last_expire = max(index for index, call in enumerate(self.api.calls) if call[1].startswith("/api/v1/node/") and call[1].endswith("/expire"))
        self.assertGreater(self.api.calls.index(marker_write), last_expire, "the marker is set only after Headscale revocation succeeded")

    def test_forbidden_user_with_marker_and_missing_journal_self_heals(self):
        self.link_headscale()
        self.run_worker()
        # A block applied by rc.2 (or a lost journal): forbidden with the worker marker, nothing durable.
        self.alice.update(isForbidden=True, groups=["employees/local-operators"])
        self.alice["properties"].update({w.FORBIDDEN_MARKER: "inactive", "headplane_role": "member"})
        self.set_frozen(True, "ou_alice")
        self.assertFalse(self.journal_path.exists())
        report = self.run_worker()
        self.assertEqual(report["revocation"]["targets"], 1)
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertTrue(self.key("3").get("expired"))
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)

    def test_held_user_without_marker_self_heals_in_staged_mode(self):
        self.link_headscale()
        self.run_worker()
        self.alice.update(isForbidden=True, groups=["employees/local-operators"])
        self.alice["properties"].update({w.HOLD_MARKER: "true", "headplane_role": "member"})
        self.config["lifecycle_mode"] = "staged"
        self.drop_status()
        self.assertFalse(self.journal_path.exists())
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply-staged")
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)

    def test_failed_revocation_is_retried_in_staged_mode(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertEqual(self.pending_subjects(), ["casdoor-alice"])
        self.api.headscale_fail_paths = set()
        self.config["lifecycle_mode"] = "staged"
        self.drop_status()
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply-staged")
        self.assertEqual(report["revocation"]["nodes_expired"], 2)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)

    def test_operator_block_without_markers_is_never_revoked(self):
        self.link_headscale()
        self.alice["isForbidden"] = True
        for _ in range(2):
            report = self.run_worker()
            self.assertEqual(report["revocation"]["targets"], 0)
        self.assertEqual(self.api.headscale_calls, [])
        self.assert_nodes_active(*ALICE_NODES)
        self.assertTrue(self.alice["isForbidden"])
        self.assertFalse(self.alice["properties"].get(REVOKED_MARKER))
        self.assertFalse(self.journal_path.exists() and self.journal()["pending"])

    def test_journal_entry_for_unapplied_block_is_dropped_without_headscale_calls(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.api.fail_update_user_for = {"employees/alice"}
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.pending_subjects(), ["casdoor-alice"], "intent is journaled before the update is sent")
        # The employee is active again before the retry: the block never applied, so no device may be revoked.
        self.set_frozen(False, "ou_alice")
        self.api.fail_update_user_for = set()
        self.api.headscale_calls.clear()
        report = self.run_worker()
        self.assertEqual(report["users_blocked"], 0)
        self.assertEqual(report["revocation"]["revoked"], 0)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(self.api.headscale_calls, [])
        self.assert_nodes_active(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assertFalse(self.alice["isForbidden"])

    def test_manual_journal_entry_for_active_user_is_dropped(self):
        self.link_headscale()
        self.run_worker()
        self.journal_path.write_text(json.dumps({"version": 1, "pending": {"casdoor-alice": {"since": int(time.time()) - 60, "source": "offboard"}}}))
        self.api.headscale_calls.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(report["revocation"]["revoked"], 0)
        self.assertEqual(self.api.headscale_calls, [])
        self.assert_nodes_active(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assertFalse(self.alice["isForbidden"])

    def test_reenable_clears_marker_so_a_later_block_revokes_again(self):
        self.config["allow_reenable"] = True
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.run_worker()
        self.assert_nodes_expired(*ALICE_NODES)
        self.assert_revoked_marker(self.alice)
        self.set_frozen(False, "ou_alice")
        report = self.run_worker()
        self.assertEqual(report["users_reenabled"], 1)
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.alice["properties"][REVOKED_MARKER], "")
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        # The employee re-enrolls devices, then leaves again.
        for node_id in ALICE_NODES:
            self.node(node_id).pop("expired", None)
        self.key("3").pop("expired", None)
        self.set_frozen(True, "ou_alice")
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertTrue(self.key("3").get("expired"))
        self.assertEqual(self.expire_node_calls(), ["11", "11", "12", "12"])
        self.assertEqual(self.api.writes.count("expire-preauthkey"), 2)
        self.assert_revoked_marker(self.alice)

    def test_second_run_after_revocation_is_idempotent(self):
        self.link_headscale()
        self.run_worker()
        self.set_frozen(True, "ou_alice")
        self.assertEqual(self.run_worker()["revocation"]["revoked"], 1)
        self.api.headscale_calls.clear()
        self.api.writes.clear()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["targets"], 0)
        self.assertEqual(report["revocation"]["revoked"], 0)
        self.assertEqual(report["revocation"]["pending"], 0)
        self.assertEqual(self.api.headscale_calls, [])
        self.assertEqual(self.api.writes, ["run-syncer"], "nothing but the native import touches Casdoor")
        self.assertEqual(self.pending_subjects(), [])

    def test_legacy_pending_revocations_are_migrated_and_processed_once(self):
        self.link_headscale()
        self.run_worker()
        # rc.2 persisted the block and the retry list in the state file after Headscale failed.
        self.alice.update(isForbidden=True, groups=["employees/local-operators"])
        self.alice["properties"].update({w.FORBIDDEN_MARKER: "inactive", "headplane_role": "member"})
        self.set_frozen(True, "ou_alice")
        legacy = json.loads(self.state.read_text())
        legacy["pending_revocations"] = ["casdoor-alice"]
        self.state.write_text(json.dumps(legacy))
        self.api.headscale_fail_paths = {"/api/v1/node"}
        with self.assertRaisesRegex(w.SyncError, "revocation"):
            self.run_worker()
        self.assertEqual(self.pending_subjects(), ["casdoor-alice"])
        self.assertEqual(self.journal()["pending"]["casdoor-alice"]["source"], "sync")
        self.assertFalse(json.loads(self.state.read_text()).get("pending_revocations"), "the legacy list is moved into the journal")
        self.api.headscale_fail_paths = set()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assert_nodes_expired(*ALICE_NODES)
        self.assertEqual(self.pending_subjects(), [])
        self.assert_revoked_marker(self.alice)
        self.api.headscale_calls.clear()
        self.assertEqual(self.run_worker()["revocation"]["targets"], 0)
        self.assertEqual(self.api.headscale_calls, [])

    # --- serialization with the scheduled worker -------------------------

    def test_serialization_offboard_fails_when_state_lock_is_held(self):
        """Serialization: offboard(apply=True) waits for the scheduled worker's state lock and gives up within lock_wait_seconds."""
        self.link_headscale()
        self.run_worker()
        self.config["lock_wait_seconds"] = 1
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = self.lock_path.open("a")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)
        self.api.writes.clear()
        started = time.monotonic()
        with self.assertRaisesRegex(w.SyncError, "lock"):
            w.offboard(self.config, "employees/alice", True, self.api)
        self.assertLess(time.monotonic() - started, 30, "the wait for the lock must be bounded by lock_wait_seconds")
        self.assertEqual(self.api.writes, [], "no Casdoor or Headscale mutation while another worker holds the lock")
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.pending_subjects(), [])
        fcntl.flock(holder, fcntl.LOCK_UN)
        report = w.offboard(self.config, "employees/alice", True, self.api)
        self.assertEqual(report["revocation"]["nodes_expired"], 2)
        self.assertEqual(self.lock_probe(), "free", "offboarding releases the lock when it returns")

    def test_serialization_watch_holds_lock_only_while_an_interval_runs(self):
        """Serialization: `run --apply --watch` holds the state lock during each run and releases it while sleeping."""
        config_path = self.state.parent / "config.json"
        config_path.write_text(json.dumps(self.config))
        observed = []
        self.api.on_request = lambda method, path, query, body: observed.append(("run", self.lock_probe()))

        def sleep(seconds):
            observed.append(("sleep", self.lock_probe()))
            raise KeyboardInterrupt

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(w, "Http", lambda *args, **kwargs: self.api), patch.object(w.time, "sleep", sleep), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = w.main(["--config", str(config_path), "--apply", "--watch"])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn('"mode": "apply"', stdout.getvalue())
        self.assertTrue(observed and observed[-1][0] == "sleep", observed)
        self.assertEqual({state for phase, state in observed if phase == "run"}, {"held"}, "the lock is held for the whole interval run")
        self.assertEqual([state for phase, state in observed if phase == "sleep"], ["free"], "the lock is released between intervals")
        self.assertEqual(self.lock_probe(), "free")


if __name__ == "__main__":
    unittest.main()
