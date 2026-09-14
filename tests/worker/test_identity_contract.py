"""Synthetic identity invariants; no signature or live OAuth claims are implied."""
import json
from pathlib import Path
import unittest


class IdentityContractTests(unittest.TestCase):
    def test_distinct_clients_preserve_casdoor_identity(self):
        path = Path(__file__).resolve().parents[2] / "sync/contracts/identity.example.json"
        fixture = json.loads(path.read_text())
        user = fixture["casdoor"]
        self.assertEqual(user["lark"], fixture["feishu"]["open_id"])
        self.assertNotEqual(user["lark"], fixture["feishu"]["user_id"])
        hs, hp = fixture["headscale_claims"], fixture["headplane_claims"]
        self.assertEqual(hs["sub"], user["id"])
        self.assertEqual(hp["sub"], user["id"])
        self.assertNotIn("/", user["id"])
        self.assertEqual(hs["iss"], hp["iss"])
        self.assertNotEqual(hs["aud"], hp["aud"])
        self.assertEqual(hs["groups"], user["groups"])
        self.assertEqual(hp["headplane_role"], "member")
        for group in user["groups"]:
            self.assertEqual(group.count("/"), 1)
            self.assertTrue(group.startswith(user["owner"] + "/feishu-"))


if __name__ == "__main__":
    unittest.main()
