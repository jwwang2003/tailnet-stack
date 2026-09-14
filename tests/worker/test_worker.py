"""Lifecycle tests with API-shaped fixtures and an in-memory Casdoor transport."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("feishu_worker", ROOT / "sync/worker.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)


def active():
    return {"is_activated": True, "is_frozen": False, "is_resigned": False, "is_exited": False}


class FixtureApi:
    def __init__(self):
        self.calls = []
        self.writes = []
        self.source_users = {"od_platform": [{"open_id": "ou_alice", "status": active()}], "od_sales": [{"open_id": "ou_bob", "status": active()}]}
        self.departments = [
            {"open_department_id": "od_platform", "name": "Platform", "parent_department_id": "od_engineering"},
            {"open_department_id": "od_engineering", "name": "Engineering", "parent_department_id": "0"},
            {"open_department_id": "od_sales", "name": "Sales", "parent_department_id": "0"},
        ]
        self.contact_groups = [{"id": "g_tailnet", "name": "Tailnet", "member_department_count": 0}, {"id": "g_admin", "name": "Administrators", "member_department_count": 0}]
        self.group_members = {"g_tailnet": ["ou_alice"], "g_admin": ["ou_alice"]}
        self.department_group_members = {}
        self.users = [{"owner": "employees", "name": "alice", "id": "casdoor-alice", "lark": "ou_alice", "groups": ["employees/local-operators"], "properties": {"local_note": "keep"}, "isForbidden": False}]
        self.groups = []
        self.scope = {"department_ids": ["0"], "user_ids": [], "group_ids": ["g_tailnet", "g_admin"]}
        self.fail_path = None
        self.native_enabled = False
        self.native_read_only = True
        self.native_columns = [{"name": "Lark", "casdoorName": "Lark", "isKey": True, "isHashed": False}, {"name": "DisplayName", "casdoorName": "DisplayName", "isHashed": True}]
        self.native_app_id = "cli_REPLACE_ME"
        self.before_update = None
        # Feishu application scope status; None simulates a denied status read.
        self.app_scopes = ["contact:contact.base:readonly", "contact:user.base:readonly", "contact:department.base:readonly",
                           "contact:user.department:readonly", "contact:user.employee:readonly", "contact:user.email:readonly",
                           "contact:user.phone:readonly", "contact:user.employee_number:read", "contact:group:readonly"]
        # Headscale fixture: users keyed by provider identifier, nodes and preauth keys.
        self.headscale_users = []
        self.headscale_nodes = []
        self.headscale_keys = []
        self.headscale_calls = []

    def page(self, key, items, more=False, cursor=""):
        return {"code": 0, "data": {key: copy.deepcopy(items), "has_more": more, "page_token": cursor}}

    def request(self, method, base, path, query=None, body=None, headers=None, retry=True):
        query = query or {}
        self.calls.append((method, path, copy.deepcopy(query)))
        if self.fail_path and self.fail_path in path:
            raise w.SyncError("fixture permission failure")
        if path == "/open-apis/application/v6/scopes":
            if self.app_scopes is None:
                return {"code": 99991672, "msg": "no permission"}
            return {"code": 0, "data": {"scopes": [{"scope_name": name, "grant_status": 1, "scope_type": "tenant"} for name in self.app_scopes]}}
        if path.startswith("/api/v1/"):
            return self.headscale(method, path, query, body, headers)
        if path.endswith("tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "synthetic-token"}
        if path.endswith("/scopes"):
            return {"code": 0, "data": {**self.scope, "has_more": False}}
        if path.endswith("/children"):
            assert query["fetch_child"] == "false"
            parent = path.split("/")[-2]
            children = [item for item in self.departments if item["parent_department_id"] == parent]
            if "page_token" not in query:
                return self.page("items", children[:1], len(children) > 1, "next/page+token=")
            if query["page_token"] != "next/page+token=":
                raise AssertionError("cursor was corrupted")
            return self.page("items", children[1:])
        if "/contact/v3/departments/" in path:
            department_id = path.split("/")[-1]
            department = next(item for item in self.departments if item["open_department_id"] == department_id)
            return {"code": 0, "data": {"department": copy.deepcopy(department)}}
        if path.endswith("users/find_by_department"):
            assert query["user_id_type"] == "open_id"
            return self.page("items", self.source_users.get(query["department_id"], []))
        if path.endswith("/group/simplelist"):
            return self.page("grouplist", self.contact_groups)
        if path.endswith("/member/simplelist"):
            group_id = path.split("/")[-3]
            if query["member_type"] == "department":
                assert query["member_id_type"] == "open_id"
                return self.page("memberlist", [{"member_id": item, "member_id_type": "open_id", "member_type": "department"} for item in self.department_group_members[group_id]])
            return self.page("memberlist", [{"member_id": item, "member_id_type": "open_id", "member_type": "user"} for item in self.group_members[group_id]])
        action = path.removeprefix("/api/")
        if action == "get-syncer":
            result = {"type": "Lark", "organization": "employees", "isEnabled": self.native_enabled, "isReadOnly": self.native_read_only, "tableColumns": self.native_columns, "host": "https://open.feishu.cn", "user": self.native_app_id}
        elif action == "get-users":
            result = copy.deepcopy(self.users)
        elif action == "get-groups":
            result = copy.deepcopy(self.groups)
        elif action == "get-user":
            # Casdoor answers a missing user with status ok and a null payload.
            result = next((user for user in self.users if user["owner"] + "/" + user["name"] == query["id"]), None)
            if result is not None and self.before_update:
                self.before_update(result)
        elif action == "run-syncer":
            self.writes.append(action)
            for user in [item for group in self.source_users.values() for item in group]:
                if not any(existing["lark"] == user["open_id"] for existing in self.users):
                    name = user["open_id"].removeprefix("ou_")
                    self.users.append({"owner": "employees", "name": name, "id": "casdoor-" + name, "lark": user["open_id"], "groups": [], "properties": {}, "isForbidden": False})
            result = None
        elif action == "add-group":
            self.writes.append(action)
            self.groups.append(copy.deepcopy(body))
            result = "Affected"
        elif action == "update-group":
            self.writes.append(action)
            old = next(item for item in self.groups if item["name"] == body["name"])
            old.update(copy.deepcopy(body))
            result = "Affected"
        elif action == "update-user":
            self.writes.append(action)
            old = next(item for item in self.users if item["owner"] + "/" + item["name"] == query["id"])
            assert set(query["columns"].split(",")) == set(body)
            assert set(body) <= {"groups", "properties", "isForbidden"}
            # Real update-user merges JSON into a shallow copy of the old user;
            # omitted Properties keys therefore remain present.
            properties = {**(old.get("properties") or {}), **body.get("properties", {})}
            old.update(copy.deepcopy(body))
            if "properties" in body:
                old["properties"] = properties
            result = "Affected"
        else:
            raise AssertionError((method, path, query))
        return {"status": "ok", "data": result}


    def headscale(self, method, path, query, body, headers):
        assert headers.get("Authorization") == "Bearer synthetic-headscale-key"
        self.headscale_calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        if path == "/api/v1/user" and method == "GET":
            return {"users": copy.deepcopy(self.headscale_users)}
        if path == "/api/v1/node" and method == "GET":
            assert not query, "node listing must not filter by user name"
            return {"nodes": copy.deepcopy(self.headscale_nodes)}
        if path.startswith("/api/v1/node/") and path.endswith("/expire") and method == "POST":
            node = next(item for item in self.headscale_nodes if item["id"] == path.split("/")[-2])
            node["expired"] = True
            self.writes.append("expire-node")
            return {"node": copy.deepcopy(node)}
        if path.startswith("/api/v1/node/") and method == "DELETE":
            self.headscale_nodes = [item for item in self.headscale_nodes if item["id"] != path.split("/")[-1]]
            self.writes.append("delete-node")
            return {}
        if path == "/api/v1/preauthkey" and method == "GET":
            return {"preAuthKeys": copy.deepcopy(self.headscale_keys)}
        if path == "/api/v1/preauthkey/expire" and method == "POST":
            key = next(item for item in self.headscale_keys if item["id"] == body["id"])
            key["expired"] = True
            self.writes.append("expire-preauthkey")
            return {}
        raise AssertionError((method, path, query))


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = json.loads((ROOT / "sync/config.example.json").read_text())
        self.config["state_file"] = str(self.directory / "state.json")
        for section, filename in (("feishu", "app_secret_file"), ("casdoor", "client_secret_file")):
            path = self.directory / section
            path.write_text("synthetic-test-secret")
            self.config[section][filename] = str(path)
        key = self.directory / "headscale_api_key"
        key.write_text("synthetic-headscale-key\n")
        self.config["headscale"]["api_key_file"] = str(key)
        self.config["feishu"]["group_ids"] = ["g_admin", "g_tailnet"]
        self.config["allow_group_id"] = "g_tailnet"
        self.config["headplane_role_groups"] = {"g_admin": "network_admin"}
        self.api = FixtureApi()

    @property
    def alice(self):
        return self.api.users[0]

    def run_worker(self, apply=True):
        return w.run(self.config, apply, self.api)

    def test_dry_run_reads_every_page_without_mutating_anything(self):
        report = self.run_worker(False)
        self.assertGreater(report["operations"], 0)
        self.assertEqual(report["unlinked_source_users"], 1)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(Path(self.config["state_file"]).exists())
        self.assertTrue(any(call[2].get("page_token") == "next/page+token=" for call in self.api.calls))

    def test_complete_sync_preserves_local_data_and_converges(self):
        report = self.run_worker()
        self.assertEqual(report["unlinked_source_users"], 0)
        self.assertEqual(self.alice["id"], "casdoor-alice")
        self.assertEqual(self.alice["properties"]["local_note"], "keep")
        self.assertEqual(self.alice["properties"]["headplane_role"], "network_admin")
        self.assertIn("employees/local-operators", self.alice["groups"])
        self.assertIn("employees/feishu-department-od_engineering", self.alice["groups"])
        self.assertIn("employees/feishu-department-od_platform", self.alice["groups"])
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        self.assertTrue(all(group["type"] == "Virtual" for group in self.api.groups))
        self.assertEqual(self.run_worker()["operations"], 0)

    def test_membership_removal_demotes_role(self):
        self.run_worker()
        self.api.group_members["g_admin"] = []
        self.run_worker()
        self.assertEqual(self.alice["properties"]["headplane_role"], "member")
        self.assertNotIn("employees/feishu-group-g_admin", self.alice["groups"])

    def test_renamed_and_moved_departments_keep_group_identity(self):
        self.run_worker()
        self.api.departments[0].update(name="New platform name", parent_department_id="od_sales")
        self.run_worker()
        group = next(group for group in self.api.groups if group["name"] == "feishu-department-od_platform")
        self.assertEqual(group["displayName"], "New platform name")
        self.assertEqual(group["parentId"], "feishu-department-od_sales")
        self.assertIn("employees/feishu-department-od_sales", self.alice["groups"])
        self.assertNotIn("employees/feishu-department-od_engineering", self.alice["groups"])

    def test_permission_failure_after_first_page_has_zero_mutations(self):
        self.api.fail_path = "users/find_by_department"
        with self.assertRaises(w.SyncError):
            self.run_worker()
        self.assertEqual(self.api.writes, [])
        self.assertFalse(Path(self.config["state_file"]).exists())

    def test_permission_scope_change_cannot_look_like_offboarding(self):
        self.run_worker()
        original_state = Path(self.config["state_file"]).read_bytes()
        self.api.writes.clear()
        self.api.scope["department_ids"] = ["od_sales"]
        with self.assertRaisesRegex(w.SyncError, "permission scope changed"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(Path(self.config["state_file"]).read_bytes(), original_state)

    def test_missing_user_needs_two_complete_applied_snapshots(self):
        self.run_worker()
        original_groups = list(self.alice["groups"])
        self.api.source_users["od_platform"] = []
        self.assertEqual(self.run_worker()["pending_missing_users"], 1)
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.alice["groups"], original_groups)
        self.run_worker(False)
        self.assertFalse(self.alice["isForbidden"])
        self.assertEqual(self.run_worker()["users_blocked"], 1)
        self.assertTrue(self.alice["isForbidden"])
        self.assertEqual(self.alice["properties"][w.FORBIDDEN_MARKER], "missing")
        self.assertEqual(self.alice["groups"], ["employees/local-operators"])
        self.assertEqual(self.alice["properties"]["headplane_role"], "member")

    def test_unknown_out_of_scope_lark_user_is_never_offboarded(self):
        self.api.users.append({"owner": "employees", "name": "external", "id": "another-id", "lark": "ou_external", "isForbidden": False})
        self.run_worker()
        self.run_worker()
        external = next(user for user in self.api.users if user["lark"] == "ou_external")
        self.assertFalse(external["isForbidden"])

    def test_inactive_reenable_and_operator_hold(self):
        self.run_worker()
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = True
        self.assertEqual(self.run_worker()["users_blocked"], 1)
        self.api.source_users["od_platform"][0]["status"]["is_frozen"] = False
        self.run_worker()
        self.assertTrue(self.alice["isForbidden"], "reenable is opt-in")
        self.config["allow_reenable"] = True
        self.alice["properties"][w.HOLD_MARKER] = "true"
        self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.alice["properties"].pop(w.HOLD_MARKER)
        self.assertEqual(self.run_worker()["users_reenabled"], 1)
        self.assertFalse(self.alice["isForbidden"])
        self.assertFalse(self.alice["properties"][w.FORBIDDEN_MARKER])
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        # A cleared worker marker must not later undo an operator's direct block.
        self.alice["isForbidden"] = True
        self.run_worker()
        self.assertTrue(self.alice["isForbidden"])

    def test_preexisting_operator_block_is_preserved(self):
        self.alice["isForbidden"] = True
        self.config["allow_reenable"] = True
        self.run_worker()
        self.assertTrue(self.alice["isForbidden"])
        self.assertNotIn("employees/tailnet-members", self.alice["groups"])

    def test_department_only_admission_needs_no_contact_group_calls(self):
        self.config["feishu"]["group_ids"] = []
        self.config["allow_group_id"] = ""
        self.config["allowed_department_ids"] = ["od_engineering"]
        self.config["headplane_role_groups"] = {}
        self.api.fail_path = "/group/"
        self.run_worker()
        self.assertIn("employees/tailnet-members", self.alice["groups"])
        bob = next(user for user in self.api.users if user["lark"] == "ou_bob")
        self.assertNotIn("employees/tailnet-members", bob["groups"])

    def test_contact_department_member_expands_child_members(self):
        self.api.contact_groups[0]["member_department_count"] = 1
        self.api.group_members["g_tailnet"] = []
        self.api.department_group_members["g_tailnet"] = ["od_engineering"]
        self.run_worker()
        self.assertIn("employees/feishu-group-g_tailnet", self.alice["groups"])
        self.assertIn("employees/tailnet-members", self.alice["groups"])

    def test_unknown_contact_department_member_aborts(self):
        self.api.contact_groups[0]["member_department_count"] = 1
        self.api.department_group_members["g_tailnet"] = ["od_unreadable"]
        with self.assertRaisesRegex(w.SyncError, "outside the fully read roots"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_duplicate_casdoor_binding_rejected_before_import(self):
        self.api.users.append({**self.alice, "id": "duplicate-id", "name": "duplicate"})
        with self.assertRaisesRegex(w.SyncError, "duplicate"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_invalid_lark_binding_rejected(self):
        self.api.source_users["od_platform"][0].pop("open_id")
        with self.assertRaisesRegex(w.SyncError, "open_id"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_native_scheduler_and_overlapping_columns_rejected(self):
        self.api.native_enabled = True
        with self.assertRaisesRegex(w.SyncError, "only scheduler"):
            self.run_worker()
        self.api.native_enabled = False
        self.api.native_columns.append({"name": "Groups", "casdoorName": "Groups"})
        with self.assertRaisesRegex(w.SyncError, "overlap"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_native_display_name_cannot_be_the_identity_key(self):
        self.api.native_columns = [{"name": "DisplayName", "casdoorName": "DisplayName", "isKey": True}]
        with self.assertRaisesRegex(w.SyncError, "immutable Lark binding key"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_native_read_only_required_before_import(self):
        self.api.native_read_only = False
        with self.assertRaisesRegex(w.SyncError, "isReadOnly=true"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_native_profile_hash_required_for_change_detection(self):
        self.api.native_columns[1].pop("isHashed")
        with self.assertRaisesRegex(w.SyncError, "isHashed=true"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_oversized_claim_rejected_before_native_import(self):
        self.alice["groups"] = ["employees/local-" + str(i) + "x" * 50 for i in range(20)]
        with self.assertRaisesRegex(w.SyncError, "1000-byte"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_different_native_app_cannot_import_foreign_identities(self):
        self.api.native_app_id = "another-app"
        with self.assertRaisesRegex(w.SyncError, "app ID must match"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])

    def test_concurrent_local_edit_aborts_instead_of_overwriting(self):
        self.api.before_update = lambda user: user["groups"].append("employees/new-local-group")
        with self.assertRaisesRegex(w.SyncError, "changed during apply"):
            self.run_worker()
        self.assertIn("employees/new-local-group", self.alice["groups"])
        self.assertNotIn("update-user", self.api.writes)
        self.assertFalse(Path(self.config["state_file"]).exists())

    def test_source_binding_change_rejected(self):
        self.run_worker()
        self.api.writes.clear()
        self.config["feishu"]["app_id"] = "another-app"
        with self.assertRaisesRegex(w.SyncError, "source binding changed"):
            self.run_worker()
        self.assertEqual(self.api.writes, [])


class HttpTests(unittest.TestCase):
    def test_rate_limit_retry_uses_bounded_retry_after_and_encodes_cursor(self):
        opener = Mock()
        response = io.BytesIO(b'{"code":0}')
        opener.open.side_effect = [HTTPError("ignored", 429, "limit", {"Retry-After": "999"}, None), response]
        sleep = Mock()
        result = w.Http(opener=opener, sleep=sleep).request("GET", "https://example.invalid", "/items", {"page_token": "next/page+token="})
        self.assertEqual(result, {"code": 0})
        sleep.assert_called_once_with(30)
        url = opener.open.call_args[0][0].full_url
        self.assertIn("next%2Fpage%2Btoken%3D", url)

    def test_mutating_requests_are_not_automatically_retried(self):
        opener = Mock()
        opener.open.side_effect = HTTPError("ignored", 503, "unavailable", {}, None)
        with self.assertRaises(w.SyncError):
            w.Http(opener=opener).request("POST", "https://example.invalid", "/api/update-user", body={}, retry=False)
        self.assertEqual(opener.open.call_count, 1)

    def test_repeated_cursor_rejected(self):
        source = w.Feishu({}, None)
        source.data = lambda path, query: {"has_more": True, "page_token": "repeat", "items": []}
        with self.assertRaisesRegex(w.SyncError, "repeated cursor"):
            source.items("items")


if __name__ == "__main__":
    unittest.main()
