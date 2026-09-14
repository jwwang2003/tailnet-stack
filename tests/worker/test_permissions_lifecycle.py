"""Scope preflight, staged population without status, and existing-access revocation."""
import copy
import json
from pathlib import Path
import unittest
import test_worker as fixtures
w = fixtures.w


class PermissionAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.WorkerLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.config, self.api = fixture.config, fixture.api
        self.run_worker = fixture.run_worker
        self.state = Path(self.config["state_file"])

    @property
    def alice(self):
        return self.api.users[0]

    def drop_status(self):
        for users in self.api.source_users.values():
            for user in users:
                user.pop("status", None)

    def link_headscale(self):
        self.api.headscale_users = [{"id": "7", "name": "alice", "providerId": "https://login.example.com/casdoor-alice"},
                                    {"id": "8", "name": "other", "providerId": "https://login.example.com/someone-else"}]
        self.api.headscale_nodes = [{"id": "11", "name": "laptop", "user": {"id": "7"}}, {"id": "12", "name": "phone", "user": {"id": "7"}},
                                    {"id": "13", "name": "server", "user": {"id": "8"}}]
        self.api.headscale_keys = [{"id": "3", "user": {"id": "7"}, "key": "redacted"}, {"id": "4", "user": {"id": "8"}, "key": "redacted"}]

    def test_permission_report_names_missing_scopes_and_grant_link(self):
        self.api.app_scopes = ["contact:contact.base:readonly", "contact:user.base:readonly", "contact:department.base:readonly",
                               "contact:user.department:readonly", "contact:user.email:readonly", "contact:user.employee_number:read"]
        self.config["employee_profile"] = {"enabled": True, "catalog_lookup": False}
        report = w.check_permissions(self.config, self.api)["permissions"]
        self.assertTrue(report["checked"])
        self.assertFalse(report["lifecycle_ready"])
        self.assertEqual(sorted(report["missing"]), ["contact_groups", "job_title", "mobile", "status"])
        self.assertEqual(report["missing_scopes"], ["contact:user.employee:readonly", "contact:group:readonly", "contact:user.phone:readonly"])
        self.assertTrue(report["grant_url"].startswith("https://open.feishu.cn/app/cli_REPLACE_ME/auth?q=contact:user.employee:readonly,contact:group:readonly,contact:user.phone:readonly&"))
        self.assertIn("token_type=tenant", report["grant_url"])
        self.api.app_scopes += ["contact:user.employee:readonly", "contact:group:readonly", "contact:user.phone:readonly"]
        report = w.check_permissions(self.config, self.api)["permissions"]
        self.assertTrue(report["lifecycle_ready"])
        self.assertEqual(report["missing"], {})
        self.assertIsNone(report["grant_url"])
        self.assertEqual(self.api.writes, [])

    def test_broad_contact_scope_satisfies_field_features(self):
        self.api.app_scopes = ["contact:contact:readonly_as_app", "contact:user.email:readonly", "contact:user.phone:readonly"]
        self.config["employee_profile"] = {"enabled": True, "catalog_lookup": False}
        self.assertEqual(w.check_permissions(self.config, self.api)["permissions"]["missing"], {})

    def test_denied_scope_status_is_reported_not_fatal(self):
        self.api.app_scopes = None
        report = w.check_permissions(self.config, self.api)["permissions"]
        self.assertFalse(report["checked"])
        self.assertIn("unavailable", report["reason"])
        audit = w.audit_directory(self.config, self.api)
        self.assertFalse(audit["permissions"]["checked"])
        self.assertEqual(audit["users_with_complete_status"], 2)

    def test_strict_mode_refuses_lifecycle_without_status(self):
        self.drop_status()
        self.api.app_scopes.remove("contact:user.employee:readonly")
        with self.assertRaisesRegex(w.SyncError, "contact:user.employee:readonly"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.state.exists())

    def test_staged_mode_populates_users_and_profiles_without_access_changes(self):
        self.config["lifecycle_mode"] = "staged"
        self.config["employee_profile"] = {"enabled": True, "catalog_lookup": False}
        self.drop_status()
        self.api.app_scopes.remove("contact:user.employee:readonly")
        self.api.source_users["od_platform"][0].update(name="Synthetic Employee", employee_no="000042")
        original = copy.deepcopy(self.alice)
        dry = self.run_worker(False)
        self.assertEqual(dry["mode"], "dry-run-staged")
        self.assertEqual(self.api.writes, [])
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply-staged")
        self.assertEqual(report["lifecycle"], "skipped")
        self.assertEqual(report["missing_scopes"], ["contact:user.employee:readonly"])
        self.assertIn("contact:user.employee:readonly", report["grant_url"])
        self.assertTrue(report["native_import"])
        self.assertEqual(report["unlinked_source_users"], 0)
        self.assertIn("run-syncer", self.api.writes)
        bob = next(user for user in self.api.users if user["lark"] == "ou_bob")
        self.assertEqual(bob["groups"], [])
        self.assertEqual(self.alice["properties"]["feishu_employee_no"], "000042")
        self.assertEqual(self.alice["properties"]["feishu_name"], "Synthetic Employee")
        self.assertEqual(self.alice["groups"], original["groups"])
        self.assertEqual(self.alice["isForbidden"], original["isForbidden"])
        self.assertNotIn("headplane_role", self.alice["properties"])
        self.assertNotIn("tailnet-members", json.dumps(self.api.users))
        self.assertFalse(self.state.exists())
        self.assertTrue(all(action in ("run-syncer", "update-user") for action in self.api.writes))
        self.assertEqual(self.run_worker()["profile_updates"], 0)

    def test_staged_mode_without_profile_enrichment_only_imports(self):
        self.config["lifecycle_mode"] = "staged"
        self.drop_status()
        report = self.run_worker()
        self.assertEqual(report["profile_updates"], 0)
        self.assertEqual(self.api.writes, ["run-syncer"])

    def test_staged_mode_upgrades_itself_once_status_is_readable(self):
        self.config["lifecycle_mode"] = "staged"
        saved = copy.deepcopy(self.api.source_users)
        self.drop_status()
        self.assertEqual(self.run_worker()["mode"], "apply-staged")
        self.api.source_users = saved
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply")
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        self.assertEqual(self.alice["properties"]["headplane_role"], "network_admin")
        self.assertTrue(self.state.exists())

    def test_staged_mode_still_rejects_scope_and_binding_changes(self):
        self.run_worker()
        self.config["lifecycle_mode"] = "staged"
        self.drop_status()
        self.api.writes.clear()
        self.api.scope["department_ids"] = ["od_sales"]
        with self.assertRaisesRegex(w.SyncError, "permission scope changed"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_block_revokes_headscale_nodes_and_keys(self):
        self.link_headscale()
        self.run_worker()
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = True
        report = self.run_worker()
        self.assertEqual(report["users_blocked"], 1)
        self.assertEqual(report["revocation"], {"configured": True, "targets": 1, "revoked": 1, "pending": 0,
                                                "headscale_users": 1, "nodes_expired": 2, "nodes_deleted": 0, "preauth_keys_expired": 1})
        expired_nodes = sorted(path.split("/")[-2] for method, path, _, _ in self.api.headscale_calls if path.endswith("/expire") and "/node/" in path)
        self.assertEqual(expired_nodes, ["11", "12"])
        self.assertEqual([body["id"] for _, path, _, body in self.api.headscale_calls if path == "/api/v1/preauthkey/expire"], ["3"])
        self.assertTrue(all(node.get("expired") for node in self.api.headscale_nodes if node["user"]["id"] == "7"))
        self.assertNotIn("expired", self.api.headscale_nodes[2])
        self.assertEqual(json.loads(self.state.read_text())["pending_revocations"], [])
        self.assertEqual(self.alice["properties"][w.REVOKED_MARKER].isdigit(), True)
        self.assertEqual(self.run_worker()["revocation"]["targets"], 0)

    def test_block_can_delete_nodes(self):
        self.link_headscale()
        self.config["headscale"]["on_block"] = "delete"
        self.run_worker()
        self.api.source_users["od_platform"] = []
        self.run_worker()
        report = self.run_worker()
        self.assertEqual(report["revocation"]["nodes_deleted"], 2)
        self.assertEqual([node["id"] for node in self.api.headscale_nodes], ["13"])

    def test_headscale_failure_keeps_block_and_retries_next_run(self):
        self.link_headscale()
        self.run_worker()
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = True
        self.api.fail_path = "/api/v1/node"
        with self.assertRaisesRegex(w.SyncError, "revocation is pending"):
            self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        journal = Path(self.config["state_file"] + ".revocations.json")
        self.assertIn("casdoor-alice", json.loads(journal.read_text())["pending"])
        self.api.fail_path = None
        report = self.run_worker()
        self.assertEqual(report["revocation"]["targets"], 1)
        self.assertEqual(report["revocation"]["revoked"], 1)
        self.assertEqual(report["revocation"]["nodes_expired"], 2)
        self.assertEqual(json.loads(journal.read_text())["pending"] if journal.exists() else {}, {})

    def test_pending_revocation_is_retried_in_staged_mode(self):
        self.link_headscale()
        self.run_worker()
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = True
        self.api.fail_path = "/api/v1/node"
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.api.fail_path = None
        self.config["lifecycle_mode"] = "staged"
        self.drop_status()
        report = self.run_worker()
        self.assertEqual(report["mode"], "apply-staged")
        self.assertEqual(report["revocation"]["nodes_expired"], 2)
        self.assertEqual(json.loads(self.state.read_text())["pending_revocations"], [])

    def test_unconfigured_headscale_reports_no_revocation(self):
        self.config["headscale"] = None
        self.run_worker()
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = True
        report = self.run_worker()
        self.assertEqual(report["revocation"], {"configured": False, "targets": 1, "revoked": 0, "pending": 1,
                                                "headscale_users": 0, "nodes_expired": 0, "nodes_deleted": 0, "preauth_keys_expired": 0})
        self.assertEqual(json.loads(self.state.read_text())["pending_revocations"], [])
        self.assertFalse(any(path.startswith("/api/v1/") for _, path, _ in self.api.calls))

    def test_offboard_blocks_and_revokes_immediately(self):
        self.link_headscale()
        self.run_worker()
        self.api.writes.clear()
        dry = w.offboard(self.config, "employees/alice", False, self.api)
        self.assertEqual(dry["mode"], "offboard-dry-run")
        self.assertEqual(dry["casdoor_updates"], ["groups", "isForbidden", "properties"])
        self.assertEqual(self.api.writes, [])
        report = w.offboard(self.config, "employees/alice", True, self.api)
        self.assertEqual(report["mode"], "offboard-apply")
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.alice["properties"][w.HOLD_MARKER], "true")
        self.assertEqual(self.alice["properties"]["headplane_role"], "member")
        self.assertEqual(self.alice["groups"], ["employees/local-operators"])
        self.assertEqual(report["revocation"]["nodes_expired"], 2)
        self.assertEqual(report["revocation"]["preauth_keys_expired"], 1)
        # The hold survives later worker runs even though the source is active.
        self.assertEqual(self.run_worker()["users_reenabled"], 0)
        self.assertTrue(self.alice["isForbidden"])
        with self.assertRaisesRegex(w.SyncError, "organization/name"):
            w.offboard(self.config, "built-in/admin", True, self.api)
        with self.assertRaisesRegex(w.SyncError, "not found"):
            w.offboard(self.config, "employees/nobody", True, self.api)

    def test_configuration_validation(self):
        path = Path(self.config["state_file"]).parent / "config.json"
        for mutate, message in ((lambda c: c.update(lifecycle_mode="lenient"), "lifecycle_mode"),
                                (lambda c: c["headscale"].update(on_block="ignore"), "on_block"),
                                (lambda c: c["headscale"].update(issuer="login.example.com"), "headscale.issuer"),
                                (lambda c: c["headscale"].update(base_url="http://user:pw@headscale:8080"), "headscale.base_url")):
            config = copy.deepcopy(self.config)
            mutate(config)
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(w.SyncError, message):
                w.load_config(path)
        config = copy.deepcopy(self.config)
        config.pop("headscale")
        config.pop("lifecycle_mode")
        path.write_text(json.dumps(config))
        loaded = w.load_config(path)
        self.assertEqual(loaded["lifecycle_mode"], "strict")
        self.assertIsNone(loaded["headscale"])


if __name__ == "__main__":
    unittest.main()
