"""Headscale existing-access revocation for blocked directory accounts. Stdlib only.

Blocking a Casdoor account stops new authentication only. This module expires
or deletes the Headscale nodes registered by the same OIDC subject and expires
the user's preauth keys, so directory offboarding also revokes enrolled devices.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import quote, urlsplit


class TailnetError(Exception):
    """Credential-free diagnostic for operator logs."""


ON_BLOCK_ACTIONS = ("expire", "delete")


def validate_headscale_config(section):
    """Normalize the optional worker `headscale` section; None disables revocation."""
    if section is None:
        return None
    if not isinstance(section, dict):
        raise TailnetError("headscale must be an object")
    for key in ("base_url", "issuer"):
        value = section.get(key)
        parts = urlsplit(value) if isinstance(value, str) else None
        if parts is None or parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise TailnetError(f"headscale.{key} must be an HTTP(S) origin without credentials or path")
        section[key] = value.rstrip("/")
    api_key_file = section.get("api_key_file")
    if not isinstance(api_key_file, str) or not api_key_file:
        raise TailnetError("headscale.api_key_file must be a nonempty path")
    if section.setdefault("on_block", "expire") not in ON_BLOCK_ACTIONS:
        raise TailnetError("headscale.on_block must be expire or delete")
    if type(section.setdefault("revoke_preauth_keys", True)) is not bool:
        raise TailnetError("headscale.revoke_preauth_keys must be boolean")
    return section


def _numeric_id(value, label):
    text = str(value) if isinstance(value, (int, str)) else ""
    if not text.isdigit():
        raise TailnetError(f"Headscale {label} ID is missing or not numeric")
    return text


class Tailnet:
    """Minimal Headscale REST client using an API key from a mounted file."""

    def __init__(self, config, http):
        self.config, self.http = config, http
        try:
            key = Path(config["api_key_file"]).read_text().strip()
        except OSError:
            raise TailnetError("cannot read the Headscale API key file") from None
        if not key:
            raise TailnetError("Headscale API key file is empty")
        self.headers = {"Authorization": "Bearer " + key}

    def call(self, method, path, query=None, body=None, mutate=False):
        result = self.http.request(method, self.config["base_url"], "/api/v1/" + path, query=query, body=body, headers=self.headers, retry=not mutate)
        if not isinstance(result, dict):
            raise TailnetError("Headscale response must be an object")
        return result

    def provider_id(self, subject):
        # Headscale derives the OIDC provider identifier as issuer + "/" + subject.
        return self.config["issuer"] + "/" + subject

    def users_for_subject(self, subject):
        users = self.call("GET", "user").get("users")
        if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
            raise TailnetError("Headscale user list is malformed")
        expected = self.provider_id(subject)
        return [user for user in users if user.get("providerId") == expected]

    def nodes_for_user(self, user_id):
        # The `user` filter of GET /api/v1/node matches user names, which are
        # not unique across providers; list every node and select by user ID.
        nodes = self.call("GET", "node").get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
            raise TailnetError("Headscale node list is malformed")
        return [node for node in nodes if str((node.get("user") or {}).get("id")) == user_id]

    def revoke(self, subject):
        """Expire or delete every node and expire every preauth key of the subject's Headscale users."""
        report = {"headscale_users": 0, "nodes_expired": 0, "nodes_deleted": 0, "preauth_keys_expired": 0}
        for user in self.users_for_subject(subject):
            user_id = _numeric_id(user.get("id"), "user")
            report["headscale_users"] += 1
            for node in self.nodes_for_user(user_id):
                node_id = _numeric_id(node.get("id"), "node")
                if self.config["on_block"] == "delete":
                    self.call("DELETE", "node/" + quote(node_id, safe=""), mutate=True)
                    report["nodes_deleted"] += 1
                else:
                    self.call("POST", "node/" + quote(node_id, safe="") + "/expire", mutate=True)
                    report["nodes_expired"] += 1
            if self.config["revoke_preauth_keys"]:
                keys = self.call("GET", "preauthkey").get("preAuthKeys")
                if not isinstance(keys, list) or any(not isinstance(key, dict) for key in keys):
                    raise TailnetError("Headscale preauth key list is malformed")
                for key in keys:
                    if str((key.get("user") or {}).get("id")) != user_id:
                        continue
                    self.call("POST", "preauthkey/expire", body={"id": _numeric_id(key.get("id"), "preauth key")}, mutate=True)
                    report["preauth_keys_expired"] += 1
        return report
