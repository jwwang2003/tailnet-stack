"""Hardening regressions against the pinned Casdoor application wire contract."""
import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("casdoor_harden", ROOT / "scripts/casdoor-harden.py")
harden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harden)


def application_round_trip(application):
    """Relevant Go Application fields and extendApplicationWithSigninMethods defaults.

    Unknown fields disappear at JSON decode. The real server unconditionally
    adds Face ID when signinMethods is empty, including when all booleans are
    false. This models that API boundary, not the hardener's desired result.
    """
    fields = {"enablePassword", "enableWebAuthn", "enableCodeSignin", "enableSignUp", "signinMethods", "providers"}
    result = {key: copy.deepcopy(value) for key, value in application.items() if key in fields}
    if not result.get("signinMethods"):
        methods = []
        for flag, name, rule in (("enablePassword", "Password", "All"),
                                 ("enableCodeSignin", "Verification code", "All"),
                                 ("enableWebAuthn", "WebAuthn", "None")):
            if result.get(flag):
                methods.append({"name": name, "displayName": name, "rule": rule})
        methods.append({"name": "Face ID", "displayName": "Face ID", "rule": "None"})
        result["signinMethods"] = methods
    return result


def face_enabled(application):
    return any(item["name"] == "Face ID" and item.get("rule") not in ("Hide password", "Hide-Password")
               for item in application["signinMethods"])


class ApplicationHardeningTests(unittest.TestCase):
    def assert_stays_hardened(self, application):
        returned = application_round_trip(application)
        self.assertFalse(face_enabled(returned))
        self.assertFalse(any(item["name"] in ("Verification code", "WebAuthn")
                             for item in returned["signinMethods"]))
        self.assertNotIn("enableFaceId", application)
        self.assertEqual(harden.plan_application(returned), [])
        return returned

    def test_real_schema_round_trip_converges(self):
        application = application_round_trip({"enablePassword": False, "enableWebAuthn": True,
                                              "enableCodeSignin": True, "enableSignUp": True})
        self.assertTrue(face_enabled(application))
        self.assertTrue(harden.plan_application(application))
        self.assert_stays_hardened(application)

    def test_false_legacy_flags_do_not_disable_present_methods(self):
        application = {"enableWebAuthn": False, "enableCodeSignin": False, "enableSignUp": False,
                       "signinMethods": [{"name": name, "rule": "None"}
                                         for name in ("Face ID", "Verification code", "WebAuthn")]}
        self.assertTrue(harden.plan_application(application))
        self.assert_stays_hardened(application)

    def test_empty_or_absent_methods_cannot_reenable_default_face_id(self):
        for methods in (None, []):
            with self.subTest(methods=methods):
                application = {"signinMethods": methods, "enablePassword": False}
                harden.plan_application(application)
                self.assert_stays_hardened(application)

    def test_removing_only_code_or_webauthn_keeps_disabled_sentinel(self):
        for name in ("Verification code", "WebAuthn"):
            with self.subTest(name=name):
                application = {"signinMethods": [{"name": name, "rule": "Hide password"}]}
                harden.plan_application(application)
                self.assert_stays_hardened(application)

    def test_retains_other_methods_and_metadata(self):
        retained = [{"name": "Password", "displayName": "Emergency password", "rule": "Hide password"},
                    {"name": "LDAP", "displayName": "Reviewed directory", "rule": "None"}]
        application = {"signinMethods": copy.deepcopy(retained) + [{"name": "Face ID", "rule": "None"}]}
        harden.plan_application(application)
        returned = self.assert_stays_hardened(application)
        self.assertEqual(returned["signinMethods"][:2], retained)

    def test_legacy_enabled_password_is_preserved(self):
        application = {"enablePassword": True, "signinMethods": []}
        harden.plan_application(application)
        returned = self.assert_stays_hardened(application)
        self.assertEqual(returned["signinMethods"], [{"name": "Password", "displayName": "Password", "rule": "All"}])

    def test_existing_oauth_only_sentinel_needs_no_write(self):
        application = {"enablePassword": False, "enableWebAuthn": False, "enableCodeSignin": False,
                       "enableSignUp": False, "providers": [{"name": "feishu", "bindingRule": [], "canSignUp": False}],
                       "signinMethods": [{"name": "Face ID", "displayName": "Face ID", "rule": "Hide password"}]}
        self.assertEqual(harden.plan_application(application), [])
        self.assert_stays_hardened(application)


if __name__ == "__main__":
    unittest.main()
