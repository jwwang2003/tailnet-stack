"""Direct-child API fixtures keep hierarchy tests independent of parent fields."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_worker import FixtureApi, ROOT, active, w


def department(key, name=None):
    return {"open_department_id": key, "name": name or key}


class TreeApi(FixtureApi):
    def __init__(self):
        super().__init__()
        engineering = department("od_engineering", "Engineering")
        platform = department("od_platform", "Platform")
        sales = department("od_sales", "Sales")
        self.root_records = {item["open_department_id"]: item for item in (engineering, platform, sales)}
        self.child_pages = {
            "0": [[engineering], [sales]],
            "od_engineering": [[platform]],
            "od_platform": [[]],
            "od_sales": [[]],
        }
        self.fail_child_page = None
        self.child_page_overrides = {}
        self.change_scope_after_read = False
        self.scope_reads = 0

    def request(self, method, base, path, query=None, body=None, headers=None, retry=True):
        query = query or {}
        if path.endswith("/scopes"):
            self.scope_reads += 1
            if self.change_scope_after_read and self.scope_reads == 2:
                self.scope["department_ids"] = ["od_sales"]
        if "/contact/v3/departments/" not in path:
            return super().request(method, base, path, query, body, headers, retry)
        self.calls.append((method, path, copy.deepcopy(query)))
        assert method == "GET"
        assert query["department_id_type"] == "open_department_id"
        if not path.endswith("/children"):
            return {"code": 0, "data": {"department": copy.deepcopy(self.root_records[path.split("/")[-1]])}}
        assert query["fetch_child"] == "false", "recursive enumeration cannot establish parent edges"
        assert query["user_id_type"] == "open_id"
        parent = path.split("/")[-2]
        cursor = query.get("page_token", "")
        page = int(cursor.removeprefix("page/+=")) if cursor else 0
        if self.fail_child_page == (parent, page):
            raise w.SyncError("fixture child-page permission failure")
        if (parent, page) in self.child_page_overrides:
            return {"code": 0, "data": copy.deepcopy(self.child_page_overrides[parent, page])}
        pages = self.child_pages[parent]
        return self.page("items", pages[page], page + 1 < len(pages), "page/+=" + str(page + 1))


class DepartmentTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = json.loads((ROOT / "sync/config.example.json").read_text())
        self.config["state_file"] = str(self.directory / "state.json")
        for section, field in (("feishu", "app_secret_file"), ("casdoor", "client_secret_file")):
            path = self.directory / section
            path.write_text("synthetic-secret")
            self.config[section][field] = str(path)
        self.config["feishu"]["group_ids"] = []
        self.config["allow_group_id"] = ""
        self.config["allowed_department_ids"] = ["od_engineering"]
        self.config["headplane_role_groups"] = {}
        self.api = TreeApi()

    def snapshot(self):
        return w.Feishu(self.config["feishu"], self.api).snapshot()

    def children_calls(self, parent):
        return [call for call in self.api.calls if call[1].endswith("/" + parent + "/children")]

    def assert_rejected_without_writes(self, message):
        with self.assertRaisesRegex(w.SyncError, message):
            w.run(self.config, apply=True, http=self.api)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(Path(self.config["state_file"]).exists())

    def test_missing_parent_fields_preserve_paginated_hierarchy(self):
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["departments"]), 4)
        self.assertEqual(snapshot["departments"]["od_platform"]["parent_department_id"], "od_engineering")
        self.assertEqual(snapshot["departments"]["od_engineering"]["parent_department_id"], "0")
        self.assertEqual(snapshot["memberships"]["ou_alice"], {"feishu-department-od_platform", "feishu-department-od_engineering"})
        self.assertEqual(len(self.children_calls("0")), 2)
        self.assertEqual(self.children_calls("0")[1][2]["page_token"], "page/+=1")
        for key in ("od_engineering", "od_platform", "od_sales"):
            self.assertEqual(len(self.children_calls(key)), 1)

    def test_four_direct_children_expand_to_45_departments_and_merge_user_memberships(self):
        tops = [department("od_top_" + str(i)) for i in range(4)]
        self.api.child_pages = {"0": [tops[:2], tops[2:]]}
        for top in tops:
            self.api.child_pages[top["open_department_id"]] = [[]]
        for index in range(41):
            leaf = department("od_leaf_" + str(index))
            parent = "od_top_" + str(index % 4)
            self.api.child_pages[parent][0].append(leaf)
            self.api.child_pages[leaf["open_department_id"]] = [[]]
        user = {"open_id": "ou_alice", "status": active()}
        self.api.source_users = {"od_leaf_0": [user], "od_leaf_1": [user], "od_top_0": [user]}
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["departments"]), 46)  # Includes synthetic root 0.
        self.assertEqual(len(snapshot["users"]), 1)
        self.assertEqual(snapshot["memberships"]["ou_alice"], {
            "feishu-department-od_leaf_0", "feishu-department-od_leaf_1",
            "feishu-department-od_top_0", "feishu-department-od_top_1",
        })
        self.assertEqual({call[1].split("/")[-2] for call in self.api.calls if call[1].endswith("/children")}, set(snapshot["departments"]))

    def test_explicit_matching_parent_is_validated(self):
        for parent, pages in self.api.child_pages.items():
            for page in pages:
                for child in page:
                    child["parent_department_id"] = parent
        self.assertEqual(self.snapshot()["departments"]["od_platform"]["parent_department_id"], "od_engineering")

    def test_leaf_without_items_is_an_empty_terminal_department_page(self):
        self.api.child_page_overrides["od_platform", 0] = {"has_more": False}
        self.api.child_page_overrides["od_sales", 0] = {"has_more": False}
        self.assertEqual(len(self.snapshot()["departments"]), 4)

    def test_empty_terminal_department_page_preserves_previous_pages(self):
        self.api.child_page_overrides["0", 1] = {"has_more": False}
        self.assertEqual(set(self.snapshot()["departments"]), {"0", "od_engineering", "od_platform"})
        self.assertEqual(len(self.children_calls("0")), 2)

    def test_missing_items_on_nonterminal_department_page_is_rejected(self):
        self.api.child_page_overrides["od_platform", 0] = {"has_more": True, "page_token": "another-page"}
        self.assert_rejected_without_writes("missing or malformed items")

    def test_explicit_malformed_department_items_are_rejected(self):
        for items in (None, {}, "", [None], ["not-a-department"]):
            with self.subTest(items=items):
                self.api.child_page_overrides["od_platform", 0] = {"has_more": False, "items": items}
                self.assert_rejected_without_writes("missing or malformed items")

    def test_missing_or_nonboolean_pagination_status_is_rejected(self):
        for page in ({}, {"has_more": None}, {"has_more": 0}, {"has_more": "false"}):
            with self.subTest(page=page):
                self.api.child_page_overrides["od_platform", 0] = page
                self.assert_rejected_without_writes("missing pagination status")

    def test_nonterminal_user_pages_still_require_explicit_item_lists(self):
        original = self.api.request

        def missing_users(method, base, path, **kwargs):
            if path.endswith("users/find_by_department"):
                return {"code": 0, "data": {"has_more": True, "page_token": "next"}}
            return original(method, base, path, **kwargs)

        self.api.request = missing_users
        self.assert_rejected_without_writes("missing or malformed items")

    def test_explicit_conflicting_or_invalid_parent_is_rejected(self):
        for parent in ("od_other", "", None, 0):
            with self.subTest(parent=parent):
                self.api.child_pages["od_engineering"][0][0]["parent_department_id"] = parent
                self.assert_rejected_without_writes("parent conflicts")

    def test_identical_duplicate_pages_do_not_repeat_subtree_reads(self):
        duplicate = copy.deepcopy(self.api.child_pages["0"][0][0])
        duplicate["parent_department_id"] = "0"
        self.api.child_pages["0"][1].insert(0, duplicate)
        self.assertEqual(len(self.snapshot()["departments"]), 4)
        self.assertEqual(len(self.children_calls("od_engineering")), 1)
        self.assertEqual(len(self.children_calls("od_platform")), 1)

    def test_conflicting_duplicate_metadata_is_rejected(self):
        self.api.child_pages["0"][1].append(department("od_engineering", "Unexpected rename"))
        self.assert_rejected_without_writes("inconsistent duplicate")

    def test_department_seen_under_two_direct_parents_is_rejected(self):
        self.api.child_pages["od_sales"] = [[department("od_platform", "Platform")]]
        self.assert_rejected_without_writes("conflicting direct parents")

    def test_direct_self_cycle_is_rejected(self):
        self.api.child_pages["od_platform"] = [[department("od_platform", "Platform")]]
        self.assert_rejected_without_writes("cyclic")

    def test_cycle_between_selected_roots_is_rejected(self):
        self.config["feishu"]["root_department_ids"] = ["od_engineering", "od_platform"]
        self.api.child_pages["od_platform"] = [[department("od_engineering", "Engineering")]]
        self.assert_rejected_without_writes("cyclic")

    def test_synthetic_root_cannot_appear_as_a_child(self):
        self.api.child_pages["od_platform"] = [[department("0", "Feishu root")]]
        self.assert_rejected_without_writes("cyclic")

    def test_overlapping_roots_keep_observed_ancestors_and_read_each_subtree_once(self):
        self.config["feishu"]["root_department_ids"] = ["od_platform", "od_engineering", "0", "od_engineering"]
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["departments"]), 4)
        self.assertEqual(snapshot["departments"]["od_platform"]["parent_department_id"], "od_engineering")
        self.assertEqual(snapshot["memberships"]["ou_alice"], {"feishu-department-od_platform", "feishu-department-od_engineering"})
        self.assertEqual(len(self.children_calls("od_engineering")), 1)
        self.assertEqual(len(self.children_calls("od_platform")), 1)

    def test_standalone_root_does_not_expand_unobserved_external_parent(self):
        self.config["feishu"]["root_department_ids"] = ["od_platform"]
        self.api.root_records["od_platform"]["parent_department_id"] = "od_outside_scope"
        snapshot = self.snapshot()
        self.assertEqual(set(snapshot["departments"]), {"od_platform"})
        self.assertEqual(snapshot["memberships"]["ou_alice"], {"feishu-department-od_platform"})
        self.assertFalse(any("od_outside_scope" in call[1] for call in self.api.calls))

    def test_overlapping_root_metadata_conflict_is_rejected(self):
        self.config["feishu"]["root_department_ids"] = ["0", "od_platform"]
        self.api.root_records["od_platform"] = {**self.api.root_records["od_platform"], "parent_department_id": "od_wrong"}
        self.assert_rejected_without_writes("inconsistent duplicate")

    def test_child_page_permission_failure_cannot_mutate_targets(self):
        self.api.fail_child_page = ("0", 1)
        self.assert_rejected_without_writes("child-page permission failure")
        self.assertEqual(len(self.children_calls("0")), 2)

    def test_permission_scope_change_during_traversal_cannot_advance_state(self):
        path = Path(self.config["state_file"])
        before = '{"version":1,"missing":{"ou_alice":1}}\n'
        path.write_text(before)
        self.api.change_scope_after_read = True
        with self.assertRaisesRegex(w.SyncError, "permission scope changed during this read"):
            w.run(self.config, apply=True, http=self.api)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
