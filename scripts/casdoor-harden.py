#!/usr/bin/env python3
"""Enforce the directory-managed Casdoor account policy for the employee organization.

Dry run by default; --apply writes. Uses the worker's Casdoor service credentials.

Policy:
- Account items that the directory owns become Admin-modifiable, so employees
  cannot edit identity, contact, profile, group, credential or biometric fields
  through the Casdoor UI/API. Language, password and MFA stay self-service.
- Every OAuth provider on the organization's applications gets an explicit
  empty binding rule: a Feishu login that is not already linked by `lark`
  cannot attach itself to an existing account by email, phone or username.
- Face ID and verification-code sign-in are disabled; the WebAuthn sign-in
  option is removed. Other configured methods, including Password/LDAP, remain
  unchanged. The pinned server's WebAuthn endpoints need separate enforcement
  before claiming that existing credentials cannot authenticate.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from deployment import load_deployment

DIRECTORY_MANAGED_ITEMS = (
    "Display name", "First name", "Last name", "Avatar", "Email", "Phone", "Country code", "Country/Region",
    "Location", "Address", "Addresses", "Affiliation", "Title", "ID card type", "ID card", "ID card info",
    "Real name", "ID verification", "Homepage", "Bio", "Gender", "Birthday", "Education", "Score", "Karma",
    "Ranking", "Managed accounts", "Face ID", "WebAuthn credentials", "MFA accounts", "3rd-party logins",
)
# Cart, Transactions and Balance items stay self-service: the pinned Casdoor
# compares a nil cart with an empty one as a change, so an Admin rule there
# would reject every ordinary profile save (for example a language switch).
ADMIN_ONLY_VIEW_ITEMS = ("Properties", "Is admin", "Is forbidden", "Is deleted", "IP whitelist", "Need update password")
SELF_SERVICE_ITEMS = ("Password", "Language", "Multi-factor authentication", "MFA items", "Consents")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit("Casdoor API redirect refused; check the configured origin")


class Api:
    def __init__(self, config):
        casdoor = config["casdoor"]
        secret = Path(casdoor["client_secret_file"]).read_text().strip()
        self.base = casdoor["base_url"].rstrip("/")
        self.headers = {"Authorization": "Basic " + base64.b64encode((casdoor["client_id"] + ":" + secret).encode()).decode(),
                        "Accept": "application/json"}
        self.opener = build_opener(NoRedirect())

    def call(self, action, query=None, body=None):
        url = self.base + "/api/" + action + ("?" + urlencode(query) if query else "")
        payload = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers, **({"Content-Type": "application/json"} if payload else {}))
        try:
            with self.opener.open(Request(url, data=payload, headers=headers, method="POST" if payload else "GET"), timeout=60) as response:
                result = json.load(response)
        except HTTPError as error:
            raise SystemExit(f"Casdoor {action} failed with HTTP {error.code}") from None
        except (URLError, OSError, ValueError):
            raise SystemExit(f"Casdoor {action} is unreachable or returned invalid JSON") from None
        if result.get("status") != "ok":
            raise SystemExit(f"Casdoor rejected {action}; inspect server logs")
        if body is not None and result.get("data") != "Affected":
            raise SystemExit(f"Casdoor {action} did not confirm a write")
        return result.get("data")


def plan_organization(organization):
    changes = []
    for item in organization.get("accountItems") or []:
        name = item.get("name")
        if name in DIRECTORY_MANAGED_ITEMS and item.get("modifyRule") != "Admin":
            changes.append(f"account item {name!r}: modifyRule {item.get('modifyRule')!r} -> 'Admin'")
            item["modifyRule"] = "Admin"
        if name in ADMIN_ONLY_VIEW_ITEMS:
            for rule in ("viewRule", "modifyRule"):
                if item.get(rule) != "Admin":
                    changes.append(f"account item {name!r}: {rule} {item.get(rule)!r} -> 'Admin'")
                    item[rule] = "Admin"
    if organization.get("isProfilePublic"):
        changes.append("isProfilePublic true -> false")
        organization["isProfilePublic"] = False
    return changes


def plan_application(application):
    changes = []
    for provider in application.get("providers") or []:
        category = (provider.get("provider") or {}).get("category")
        if category in (None, "OAuth", "SAML") and provider.get("bindingRule") != []:
            changes.append(f"provider {provider.get('name')!r}: bindingRule {provider.get('bindingRule')!r} -> [] (no email/phone/name fallback binding)")
            provider["bindingRule"] = []
        for flag in ("canSignUp",):
            if provider.get(flag):
                changes.append(f"provider {provider.get('name')!r}: {flag} true -> false")
                provider[flag] = False
    # These are the actual legacy fields in the pinned Application schema.
    # Face ID has no boolean flag: its signinMethods entry controls access.
    for flag in ("enableWebAuthn", "enableCodeSignin", "enableSignUp"):
        if application.get(flag) is not False:
            changes.append(f"{flag} {application.get(flag)!r} -> false")
            application[flag] = False
    methods = application.get("signinMethods") or []
    hardened = []
    if not methods and application.get("enablePassword"):
        # Match Casdoor's legacy empty-list expansion without dropping a
        # password method that was already enabled before hardening.
        hardened.append({"name": "Password", "displayName": "Password", "rule": "All"})
    for method in methods:
        if method.get("name") in ("Verification code", "WebAuthn"):
            # Verification-code backend checks do not honor the hidden rule.
            continue
        item = dict(method)
        if item.get("name") == "Face ID":
            item["rule"] = "Hide password"
        hardened.append(item)
    if not hardened:
        # Casdoor adds an ENABLED Face ID method when the list is empty.
        # Keep an explicitly disabled entry to suppress that default.
        hardened.append({"name": "Face ID", "displayName": "Face ID", "rule": "Hide password"})
    if hardened != methods:
        changes.append("signinMethods: disable Face ID and remove WebAuthn/verification-code sign-in")
        application["signinMethods"] = hardened
    return changes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="worker configuration (Casdoor origin, organization, service credentials)")
    parser.add_argument("--application", action="append", default=[], metavar="OWNER/NAME",
                        help="application to harden; default: every non-service application of the organization")
    parser.add_argument("--deployment", type=Path, help="Deployment descriptor; external mode requires explicit applications")
    parser.add_argument("--include-organization", action="store_true",
                        help="Also change shared organization policy when selecting applications")
    parser.add_argument("--apply", action="store_true", help="write the planned changes")
    args = parser.parse_args(argv)
    deployment = load_deployment(args.deployment)
    external = deployment['identity']['mode'] == 'external'
    if external and not args.application:
        parser.error('External identity hardening requires explicit --application OWNER/NAME')
    config = json.loads(Path(args.config).read_text())
    organization_name = config["casdoor"]["organization"]
    api = Api(config)
    plans = []
    # Explicit application selection never implicitly changes all employees' policy.
    if args.include_organization or (not external and not args.application):
        organization = api.call("get-organization", {"id": "admin/" + organization_name})
        if not isinstance(organization, dict):
            raise SystemExit("organization not found")
        plans.append(("organization admin/" + organization_name, organization, plan_organization(organization), "update-organization", {"id": "admin/" + organization_name}))
    applications = api.call("get-applications", {"owner": "admin"}) or []
    available = {app['owner'] + '/' + app['name'] for app in applications
                 if app.get('organization') == organization_name}
    if set(args.application) - available:
        raise SystemExit('Requested application is missing or outside the configured organization')
    for application in applications:
        if application.get("organization") != organization_name:
            continue
        qualified = application["owner"] + "/" + application["name"]
        if args.application and qualified not in args.application:
            continue
        grants = application.get("grantTypes") or []
        if not args.application and grants and set(grants) <= {"client_credentials"}:
            continue  # server-to-server credentials, no interactive login
        if application.get("clientId") == config["casdoor"]["client_id"] and not args.application:
            continue
        plans.append(("application " + qualified, application, plan_application(application), "update-application", {"id": qualified}))
    total = 0
    for label, obj, changes, action, query in plans:
        print(label + ":", "no change" if not changes else "")
        for change in changes:
            print("  -", change)
        total += len(changes)
        if changes and args.apply:
            api.call(action, query, obj)
            print("  applied")
    print("Planned changes:", total, "(applied)" if args.apply else "(dry run; rerun with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
