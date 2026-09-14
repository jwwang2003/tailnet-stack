#!/usr/bin/env python3
"""Prove that an ordinary employee session cannot change authorization fields in Casdoor.

Creates a temporary password-login application, group and user inside the
employee organization, signs in as that user, attempts the partial-update
bypasses (properties, groups, isAdmin, isForbidden, email, full-object update)
and verifies through the administrative API that nothing changed. Temporary
objects are deleted afterwards unless --keep is given. Exit status 1 on any
authorization failure. Run it against a candidate image before promotion.
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit("Casdoor API redirect refused; check the configured origin")


def request(opener, base, action, query=None, body=None, headers=None):
    url = base + "/api/" + action + ("?" + urlencode(query) if query else "")
    payload = None if body is None else json.dumps(body).encode()
    merged = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    try:
        with opener.open(Request(url, data=payload, headers=merged, method="POST" if payload is not None else "GET"), timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        return {"status": "error", "msg": f"HTTP {error.code}"}
    except (URLError, OSError, ValueError):
        raise SystemExit(f"Casdoor {action} is unreachable or returned invalid JSON") from None


class Admin:
    def __init__(self, config):
        casdoor = config["casdoor"]
        secret = Path(casdoor["client_secret_file"]).read_text().strip()
        self.base = casdoor["base_url"].rstrip("/")
        self.headers = {"Authorization": "Basic " + base64.b64encode((casdoor["client_id"] + ":" + secret).encode()).decode()}
        self.opener = build_opener(NoRedirect())

    def call(self, action, query=None, body=None, check=True):
        result = request(self.opener, self.base, action, query, body, self.headers)
        if check and result.get("status") != "ok":
            raise SystemExit(f"administrative {action} failed: {result.get('msg', '')[:200]}")
        return result.get("data")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="worker configuration with Casdoor service credentials")
    parser.add_argument("--public-url", help="Casdoor origin for the employee session (default: worker casdoor.base_url)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary objects for inspection")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    organization = config["casdoor"]["organization"]
    admin = Admin(config)
    public = (args.public_url or config["casdoor"]["base_url"]).rstrip("/")
    tag = secrets.token_hex(4)
    app_name, group_name, user_name = f"authz-probe-{tag}", f"authz-probe-group-{tag}", f"authz-probe-{tag}"
    password = secrets.token_urlsafe(24)
    template = admin.call("get-application", {"id": "admin/app-built-in"})
    application = {**template, "owner": "admin", "name": app_name, "displayName": "Authorization probe (temporary)", "organization": organization,
                   "clientId": "probe-" + tag, "clientSecret": secrets.token_hex(20), "enablePassword": True, "enableSignUp": False,
                   "enableCodeSignin": False, "enableWebAuthn": False, "enableFaceId": False, "isShared": False, "providers": [],
                   "signinMethods": [{"name": "Password", "displayName": "Password", "rule": "All"}], "grantTypes": ["authorization_code"],
                   "redirectUris": [], "tokenFormat": "JWT", "tokenFields": [], "tokenAttributes": [], "tags": [], "enableCaptcha": False}
    created = []
    failures = []
    try:
        admin.call("add-application", {"id": "admin/" + app_name}, application)
        # Casdoor deletes an application only when the organization is also given.
        created.append(("delete-application", {"owner": "admin", "name": app_name, "organization": organization}))
        admin.call("add-group", {"id": organization + "/" + group_name}, {"owner": organization, "name": group_name, "displayName": "Authorization probe group",
                                                                          "type": "Virtual", "parentId": organization, "isTopGroup": True, "isEnabled": True})
        created.append(("delete-group", {"owner": organization, "name": group_name}))
        admin.call("add-user", None, {"owner": organization, "name": user_name, "displayName": "Authorization probe", "password": password,
                                      "type": "normal-user", "signupApplication": app_name, "email": f"{user_name}@probe.invalid",
                                      "properties": {"headplane_role": "member"}, "groups": [organization + "/" + group_name], "isAdmin": False, "isForbidden": False})
        created.append(("delete-user", {"owner": organization, "name": user_name}))
        target = organization + "/" + user_name
        baseline = admin.call("get-user", {"id": target})
        if not isinstance(baseline, dict) or baseline.get("groups") != [organization + "/" + group_name]:
            raise SystemExit("probe user was not created with the expected group")
        jar = http.cookiejar.CookieJar()
        session = build_opener(NoRedirect(), HTTPCookieProcessor(jar))
        login = request(session, public, "login", None, {"application": app_name, "organization": organization, "username": user_name, "password": password,
                                                          "autoSignin": True, "signinMethod": "Password", "type": "login"})
        if login.get("status") != "ok":
            raise SystemExit("employee session login failed: " + str(login.get("msg", ""))[:200])
        account = request(session, public, "get-account")
        if account.get("status") != "ok" or (account.get("data") or {}).get("name") != user_name:
            raise SystemExit("employee session does not resolve to the probe user")
        full = {**baseline, "properties": {"headplane_role": "admin"}, "groups": [organization + "/" + config["admission_group"]], "isAdmin": True}
        attempts = [
            ("properties via columns", {"columns": "properties"}, {"properties": {"headplane_role": "admin", "feishu_sync_hold": ""}}, "properties", baseline.get("properties")),
            ("groups via columns (same length)", {"columns": "groups"}, {"groups": [organization + "/" + config["admission_group"]]}, "groups", baseline.get("groups")),
            ("isAdmin via columns", {"columns": "isAdmin"}, {"isAdmin": True}, "isAdmin", False),
            ("isForbidden via columns", {"columns": "isForbidden"}, {"isForbidden": True}, "isForbidden", False),
            ("email via columns", {"columns": "email"}, {"email": "impersonate@probe.invalid"}, "email", baseline.get("email")),
            ("full object update", {}, full, "properties", baseline.get("properties")),
            ("full object update (groups)", {}, full, "groups", baseline.get("groups")),
            ("full object update (isAdmin)", {}, full, "isAdmin", False),
        ]
        for label, extra, body, field, expected in attempts:
            response = request(session, public, "update-user", {"id": target, **extra}, body)
            latest = admin.call("get-user", {"id": target})
            actual = latest.get(field)
            verdict = "PASS" if actual == expected else "FAIL"
            if verdict == "FAIL":
                failures.append(label)
            print(f"{verdict}: {label}: server said {response.get('status')} ({str(response.get('msg', ''))[:80]}); {field} is {'unchanged' if actual == expected else 'CHANGED to ' + json.dumps(actual)}")
            if actual != expected:
                # Restore the whole baseline object (a columns update would merge maps).
                admin.call("update-user", {"id": target}, baseline)
    finally:
        if args.keep:
            print("Temporary objects kept:", [entry[1] for entry in created])
        else:
            for action, obj in reversed(created):
                admin.call(action, None, obj, check=False)
    if failures:
        print("Authorization bypass detected:", failures)
        return 1
    print("All ordinary-user authorization checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
