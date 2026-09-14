"""Synthetic fixtures for descriptive metadata; no employee/API access."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("employee_profile", ROOT / "sync/employee_profile.py")
p = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p)


class EmployeeProfileTests(unittest.TestCase):
    def setUp(self):
        self.departments = {
            "od_company": {"name": "Company", "parent_department_id": "0"},
            "od_eng": {"name": "Engineering", "parent_department_id": "od_company"},
            "od_platform": {"name": "Platform", "parent_department_id": "od_eng"},
            "od_sales": {"name": "Sales", "parent_department_id": "od_company"},
        }

    def test_normalize_preserves_explicit_empty_false_and_zero(self):
        user = {"name": " Example Employee ", "mobile": "", "email": None,
                "employee_type": 0, "is_tenant_manager": False}
        self.assertEqual(p.normalize_profile(user), {"name": "Example Employee", "mobile": "",
                                                    "employee_type": 0, "is_tenant_manager": False})

    def test_unknown_and_custom_fields_are_never_captured(self):
        user = {"job_level_id": "level_5", "ranking": 5, "isAdmin": True,
                "roles": ["admin"], "headplane_role": "admin",
                "custom_attrs": [{"id": "sensitive", "value": {"text": "private-value"}}]}
        self.assertEqual(p.normalize_profile(user), {"job_level_id": "level_5"})
        self.assertEqual(p.profile_property_delta(user, {}), {"feishu_job_level_id": "level_5"})
        self.assertEqual(p.normalize_profile({"custom_attrs": "malformed-but-disabled"}), {})

    def test_wrong_types_fail_with_value_free_errors(self):
        for field, value in (("mobile", ["private-value"]), ("email", {"private-value": 1}),
                             ("employee_type", True), ("employee_type", -1),
                             ("is_tenant_manager", "private-value"), ("job_level_id", "private value")):
            with self.subTest(field=field):
                with self.assertRaises(p.ProfileError) as error:
                    p.normalize_profile({field: value})
                self.assertNotIn("private", str(error.exception))
        with self.assertRaises(p.ProfileError):
            p.normalize_profile({"name": "a" * 4097})
        with self.assertRaises(p.ProfileError):
            p.normalize_profile({"name": "private-value\ud800"})

    def test_duplicate_reads_merge_missing_but_reject_conflicts(self):
        first = {"email": "employee@example.invalid", "mobile": ""}
        second = {"email": "employee@example.invalid", "job_title": "Engineer", "mobile": None}
        self.assertEqual(p.merge_profiles(first, second), {**first, "job_title": "Engineer"})
        self.assertEqual(first, {"email": "employee@example.invalid", "mobile": ""})
        for field, value in (("email", "different@example.invalid"), ("mobile", "+10000000000")):
            with self.assertRaisesRegex(p.ProfileError, "inconsistent duplicate"):
                p.merge_profiles(first, {field: value})

    def test_delta_contains_only_changed_owned_properties(self):
        original = {"local_note": "keep", "oauth_lark_accessToken": "***",
                    "headplane_role": "viewer", "feishu_sync_hold": "true",
                    "feishu_mobile": "+10000000000", "feishu_email": "old@example.invalid"}
        saved = copy.deepcopy(original)
        delta = p.profile_property_delta({"mobile": None, "email": "", "is_tenant_manager": True,
                                          "leader_user_id": "ou_manager"}, original)
        self.assertEqual(delta, {"feishu_email": "", "feishu_is_tenant_manager": "true",
                                 "feishu_manager_open_id": "ou_manager"})
        self.assertTrue(set(delta) <= p.OWNED_PROPERTY_KEYS)
        self.assertEqual(original, saved)
        self.assertEqual(p.profile_property_delta({"email": ""}, original | delta), {})

    def test_catalog_names_only_come_from_successful_lookup(self):
        existing = {"feishu_job_level_id": "level_5", "feishu_job_level_name": "Previously resolved"}
        profile = {"job_level_id": "level_5", "job_family_id": "family_eng"}
        delta = p.profile_property_delta(profile, existing, job_levels={}, job_families={"family_eng": "Engineering"})
        self.assertNotIn("feishu_job_level_name", delta)
        self.assertEqual(delta["feishu_job_family_name"], "Engineering")
        self.assertEqual(p.profile_property_delta(profile, {}, job_levels={"level_5": "Level 5"})["feishu_job_level_name"], "Level 5")
        self.assertEqual(p.profile_property_delta({"job_level_id": ""}, existing),
                         {"feishu_job_level_id": "", "feishu_job_level_name": ""})
        with self.assertRaises(p.ProfileError):
            p.profile_property_delta(profile, {}, job_levels={"level_5": 5})

    def test_changed_catalog_id_clears_stale_name_but_absent_id_preserves_it(self):
        for field in ("job_level", "job_family"):
            with self.subTest(field=field):
                existing = {"feishu_" + field + "_id": "old_id", "feishu_" + field + "_name": "Old name"}
                delta = p.profile_property_delta({field + "_id": "new_id"}, existing)
                self.assertEqual(delta, {"feishu_" + field + "_id": "new_id", "feishu_" + field + "_name": ""})
                self.assertEqual(p.profile_property_delta({}, existing), {})

    def test_direct_memberships_have_stable_full_paths_without_extra_memberships(self):
        metadata = p.department_metadata(["od_sales", "od_platform", "od_platform"], self.departments)
        records = json.loads(metadata["feishu_departments"])
        self.assertEqual([record["id"] for record in records], ["od_platform", "od_sales"])
        self.assertEqual(records[0]["path"], [{"id": "od_company", "name": "Company"},
                                             {"id": "od_eng", "name": "Engineering"},
                                             {"id": "od_platform", "name": "Platform"}])
        self.assertEqual(metadata, p.department_metadata({"od_platform", "od_sales"}, self.departments))
        self.assertNotIn('"id":"0"', metadata["feishu_departments"])

    def test_selected_roots_stop_outside_snapshot_and_empty_is_explicit(self):
        departments = {"od_eng": {"name": "Engineering", "parent_department_id": "outside"},
                       "od_platform": self.departments["od_platform"]}
        result = p.department_metadata(["od_platform"], departments, ["od_eng"])
        self.assertEqual(len(json.loads(result["feishu_departments"])[0]["path"]), 2)
        self.assertEqual(p.department_metadata(["0"], {}), {"feishu_departments": "[]"})
        existing = {"feishu_departments": "old metadata"}
        self.assertEqual(p.profile_property_delta({}, existing), {})
        self.assertEqual(p.profile_property_delta({}, existing, direct_department_ids=[], departments={}),
                         {"feishu_departments": "[]"})

    def test_missing_cyclic_or_unselected_ancestry_fails(self):
        for mutation in ("missing", "cycle", "unselected", "name"):
            with self.subTest(mutation=mutation):
                departments = copy.deepcopy(self.departments)
                roots = ["0"]
                if mutation == "missing":
                    del departments["od_eng"]
                elif mutation == "cycle":
                    departments["od_eng"]["parent_department_id"] = "od_platform"
                elif mutation == "unselected":
                    roots = ["od_sales"]
                else:
                    departments["od_eng"]["name"] = ""
                with self.assertRaises(p.ProfileError):
                    p.department_metadata(["od_platform"], departments, roots)

    def test_department_rename_and_move_change_only_metadata(self):
        old = p.department_metadata(["od_platform"], self.departments)
        self.departments["od_platform"].update(name="Infrastructure", parent_department_id="od_sales")
        delta = p.profile_property_delta({}, old, direct_department_ids=["od_platform"], departments=self.departments)
        self.assertEqual(set(delta), {"feishu_departments"})
        record = json.loads(delta["feishu_departments"])[0]
        self.assertEqual(record["name"], "Infrastructure")
        self.assertEqual(record["path"][-2]["id"], "od_sales")

    def test_coverage_reports_counts_and_no_employee_values(self):
        report = p.profile_coverage([{"name": "Private Example", "mobile": "+10000000000", "email": ""},
                                     {"mobile": None, "employee_type": 0, "is_tenant_manager": False}])
        self.assertEqual(report["users"], 2)
        self.assertEqual(report["fields"]["mobile"], {"available": 1, "populated": 1})
        self.assertEqual(report["fields"]["email"], {"available": 1, "populated": 0})
        self.assertEqual(report["fields"]["employee_type"]["populated"], 1)
        self.assertEqual(report["fields"]["is_tenant_manager"]["populated"], 1)
        serialized = json.dumps(report)
        self.assertNotIn("Private Example", serialized)
        self.assertNotIn("+10000000000", serialized)


if __name__ == "__main__":
    unittest.main()

class EmployeeNumberTests(unittest.TestCase):
    def test_employee_number_is_text_not_an_internal_id_or_rank(self):
        profile = p.normalize_profile({"open_id":"ou_person", "user_id":"internal", "employee_no":"000042"})
        self.assertEqual(profile, {"employee_no":"000042"})
        delta = p.profile_property_delta(profile, {})
        self.assertEqual(delta, {"feishu_employee_no":"000042"})
