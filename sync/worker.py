#!/usr/bin/env python3
"""Feishu Contact directory → existing Casdoor users. Python 3.11+, stdlib only.

Dry-run by default. A complete source read precedes every mutation. Native
Casdoor import runs serially; its scheduler and overlapping columns are rejected.
"""
from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener


class SyncError(Exception):
    """Safe, credential-free diagnostic suitable for operator logs."""


PREFIXES = ("feishu-department-", "feishu-group-")
FORBIDDEN_MARKER = "feishu_sync_forbidden"
HOLD_MARKER = "feishu_sync_hold"
OWNER_MARKER = "feishu_sync_owner"
WORKER_OWNER = "tailscale-feishu-integration/v1"
ROLE_PRIORITY = ("admin", "network_admin", "it_admin", "auditor", "viewer", "member")
LIFECYCLE_MODES = ("strict", "staged")
# Feishu Contact field permissions (any one scope per feature suffices). Names
# follow the Contact v3 "获取单个用户信息" field permission table.
BROAD_CONTACT_SCOPES = ("contact:contact:readonly_as_app", "contact:contact:readonly", "contact:contact:access_as_app")
FEATURE_SCOPES = {
    "directory_read": ("contact:contact.base:readonly", *BROAD_CONTACT_SCOPES),
    "department_tree": ("contact:department.base:readonly", *BROAD_CONTACT_SCOPES),
    "user_base": ("contact:user.base:readonly", *BROAD_CONTACT_SCOPES),
    "user_department": ("contact:user.department:readonly", *BROAD_CONTACT_SCOPES),
    "status": ("contact:user.employee:readonly", *BROAD_CONTACT_SCOPES),
    "job_title": ("contact:user.employee:readonly", *BROAD_CONTACT_SCOPES),
    "employee_no": ("contact:user.employee_number:read", "contact:user.employee:readonly", *BROAD_CONTACT_SCOPES),
    "email": ("contact:user.email:readonly",),
    "mobile": ("contact:user.phone:readonly",),
    "tenant_name": ("tenant:tenant:readonly",),
    "contact_groups": ("contact:group:readonly", *BROAD_CONTACT_SCOPES),
    "job_level": ("contact:job_level:readonly", "contact:job_level", "contact:contact:readonly_as_app"),
    "job_family": ("contact:job_family:readonly", "contact:job_family", "contact:contact:readonly_as_app"),
}
LIFECYCLE_FEATURES = ("directory_read", "department_tree", "user_base", "user_department", "status")
DESCRIPTIVE_FEATURES = ("employee_no", "job_title", "email", "mobile")

# Support both direct CLI execution and import-based test/automation callers.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from employee_profile import (ProfileError, normalize_profile, merge_profiles,
                              profile_property_delta, profile_coverage)
from tailnet import Tailnet, TailnetError, validate_headscale_config, revoke_subjects


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise SyncError(f"invalid or missing {label}")
    return value


def secret(path):
    try:
        result = Path(path).read_text().strip()
    except OSError:
        raise SyncError("cannot read configured secret file") from None
    if not result:
        raise SyncError("secret file is empty")
    return result


def load_config(path):
    try:
        config = json.loads(Path(path).read_text())
        fs, cs = config["feishu"], config["casdoor"]
        for entry in (fs, cs):
            parts = urlsplit(entry["base_url"])
            if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
                raise SyncError("base_url must be an HTTP(S) origin without credentials")
            entry["base_url"] = entry["base_url"].rstrip("/")
        if fs["base_url"] not in ("https://open.feishu.cn", "https://open.larksuite.com"):
            raise SyncError("Feishu base_url must select the official Feishu or Lark endpoint")
        identifier(cs["organization"], "Casdoor organization")
        identifier(config["admission_group"], "admission group")
        if not fs["app_id"] or not fs["tenant_key"] or not cs["client_id"]:
            raise SyncError("app, tenant and client identifiers must be set")
        if not isinstance(fs["root_department_ids"], list) or not fs["root_department_ids"]:
            raise SyncError("at least one explicit root department is required")
        if not isinstance(fs["group_ids"], list):
            raise SyncError("group_ids must be an array (empty for department-only admission)")
        for key in ("root_department_ids", "group_ids"):
            fs[key] = sorted(set(identifier(item) for item in fs[key]))
        profile = config.setdefault("employee_profile", {})
        if not isinstance(profile, dict):
            raise SyncError("employee_profile must be an object")
        for key in ("enabled", "catalog_lookup"):
            if type(profile.setdefault(key, False)) is not bool:
                raise SyncError("employee profile options must be boolean")
        expected_tenant = fs.get("expected_tenant_name")
        if expected_tenant is not None and (not isinstance(expected_tenant, str) or not expected_tenant.strip()):
            raise SyncError("expected_tenant_name must be a nonempty string")
        config.setdefault("allow_group_id", "")
        if config["allow_group_id"] and config["allow_group_id"] not in fs["group_ids"]:
            raise SyncError("allow_group_id must be in feishu.group_ids")
        allowed_departments = config.setdefault("allowed_department_ids", [])
        if not isinstance(allowed_departments, list):
            raise SyncError("allowed_department_ids must be an array")
        config["allowed_department_ids"] = sorted(set(identifier(item) for item in allowed_departments))
        if "0" in allowed_departments:
            raise SyncError("select explicit admission departments instead of root 0")
        if not config["allow_group_id"] and not allowed_departments:
            raise SyncError("select at least one admission group or department")
        role_groups = config.setdefault("headplane_role_groups", {})
        if not isinstance(role_groups, dict) or any(group not in fs["group_ids"] or role not in ROLE_PRIORITY for group, role in role_groups.items()):
            raise SyncError("headplane_role_groups must map selected Contact group IDs to valid roles")
        if not cs["native_syncer_id"] or cs["native_syncer_id"].count("/") != 1:
            raise SyncError("native_syncer_id must be owner/name")
        for field, default, minimum, maximum in (("interval_seconds", 300, 30, 86400), ("missing_confirmations", 2, 2, 100), ("http_timeout_seconds", 30, 1, 120), ("http_attempts", 3, 1, 5)):
            value = config.setdefault(field, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise SyncError(f"invalid {field}")
        if type(config.setdefault("allow_reenable", False)) is not bool:
            raise SyncError("allow_reenable must be boolean")
        if config.setdefault("lifecycle_mode", "strict") not in LIFECYCLE_MODES:
            raise SyncError("lifecycle_mode must be strict or staged")
        try:
            config["headscale"] = validate_headscale_config(config.get("headscale"))
        except TailnetError as error:
            raise SyncError(str(error)) from None
        for field in (fs["app_secret_file"], cs["client_secret_file"], config["state_file"]):
            if not isinstance(field, str) or not field:
                raise SyncError("file paths must be nonempty")
    except (KeyError, TypeError, ValueError, OSError):
        raise SyncError("invalid or unreadable worker configuration") from None
    return config


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("API redirect refused; check the configured origin")


class Http:
    def __init__(self, timeout=30, attempts=3, opener=None, sleep=time.sleep):
        self.timeout, self.attempts = timeout, attempts
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep

    def request(self, method, base, path, query=None, body=None, headers=None, retry=True):
        url = base + path + (("?" + urlencode(query)) if query else "")
        payload = None if body is None else json.dumps(body).encode()
        merged_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            merged_headers["Content-Type"] = "application/json"
        attempts = self.attempts if retry else 1
        for attempt in range(attempts):
            try:
                req = Request(url, data=payload, headers=merged_headers, method=method)
                with self.opener.open(req, timeout=self.timeout) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise SyncError("API response exceeded size limit")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise SyncError("API response must be an object")
                return result
            except HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code < 600
                if not retryable or attempt == attempts - 1:
                    raise SyncError(f"API HTTP {error.code} on {path}") from None
                retry_after = error.headers.get("Retry-After", "") if error.headers else ""
                delay = min(int(retry_after), 30) if retry_after.isdigit() else min(2 ** attempt, 8)
                self.sleep(delay)
            except (URLError, TimeoutError, ConnectionError, OSError) as error:
                if attempt == attempts - 1:
                    # The OS-level reason (DNS, refused, timeout) carries no credentials.
                    reason = getattr(error, "reason", None) or error
                    raise SyncError(f"API transport failure on {path}: {type(reason).__name__}: {str(reason)[:120]}") from None
                self.sleep(min(2 ** attempt, 8))
            except (ValueError, UnicodeDecodeError):
                raise SyncError(f"invalid API JSON on {path}") from None
        raise SyncError("API request failed")


class Feishu:
    def __init__(self, config, http):
        self.config, self.http, self.token = config, http, None

    def authenticate(self):
        response = self.http.request("POST", self.config["base_url"], "/open-apis/auth/v3/tenant_access_token/internal", body={"app_id": self.config["app_id"], "app_secret": secret(self.config["app_secret_file"])})
        if response.get("code") != 0 or not response.get("tenant_access_token"):
            raise SyncError("Feishu app authentication failed")
        self.token = response["tenant_access_token"]

    def data(self, path, query=None):
        result = self.http.request("GET", self.config["base_url"], "/open-apis/contact/v3/" + path, query=query, headers={"Authorization": "Bearer " + self.token})
        if result.get("code") != 0:
            raise SyncError(f"Feishu read failed on {path}; code={result.get('code')!r}")
        if not isinstance(result.get("data"), dict):
            raise SyncError(f"Feishu data missing on {path}")
        return result["data"]

    def pages(self, path, query=None):
        query = {"page_size": 50, **(query or {})}
        seen = set()
        for _ in range(10000):
            data = self.data(path, query)
            if type(data.get("has_more")) is not bool:
                raise SyncError(f"missing pagination status on {path}")
            yield data
            if not data["has_more"]:
                return
            cursor = data.get("page_token")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise SyncError(f"invalid or repeated cursor on {path}")
            seen.add(cursor)
            query["page_token"] = cursor
        raise SyncError("pagination limit exceeded")

    def items(self, path, key="items", query=None, *, allow_empty_terminal=False):
        result = []
        for page in self.pages(path, query):
            # Feishu's department-children endpoint omits items for empty
            # terminal pages. Other endpoints retain strict list validation.
            if allow_empty_terminal and key not in page and page["has_more"] is False:
                continue
            items = page.get(key)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise SyncError(f"missing or malformed {key} on {path}")
            result.extend(items)
        return result

    def scope(self):
        result = {key: set() for key in ("department_ids", "user_ids", "group_ids")}
        for page in self.pages("scopes", {"user_id_type": "open_id", "department_id_type": "open_department_id"}):
            for key in result:
                values = page.get(key, [])
                if not isinstance(values, list):
                    raise SyncError("malformed Contact permission scope")
                result[key].update(identifier(value) for value in values)
        return {key: sorted(values) for key, values in result.items()}

    def required_features(self, config):
        """Features the configuration relies on, in reporting order."""
        features = list(LIFECYCLE_FEATURES)
        if config["feishu"]["group_ids"]:
            features.append("contact_groups")
        if config["feishu"].get("expected_tenant_name"):
            features.append("tenant_name")
        profile = config.get("employee_profile", {})
        if profile.get("enabled"):
            features.extend(DESCRIPTIVE_FEATURES)
            if profile.get("catalog_lookup"):
                features.extend(("job_level", "job_family"))
        return features

    def grant_url(self, scopes):
        # Same link shape Feishu returns in its 99991672 permission errors.
        return self.config["base_url"] + "/app/" + quote(self.config["app_id"], safe="") + "/auth?q=" + quote(",".join(scopes), safe=",:_.") + "&op_from=openapi&token_type=tenant"

    def permissions(self, config):
        """Report granted/missing app scopes per feature; never raises on a denied status read."""
        features = self.required_features(config)
        result = self.http.request("GET", self.config["base_url"], "/open-apis/application/v6/scopes", headers={"Authorization": "Bearer " + self.token})
        report = {"checked": False, "granted": [], "missing": {}, "missing_scopes": [], "grant_url": None}
        scopes = result.get("data", {}).get("scopes") if result.get("code") == 0 else None
        if not isinstance(scopes, list):
            report["reason"] = f"scope status unavailable; code={result.get('code')!r}"
            return report
        granted = set()
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get("scope_name"), str):
                raise SyncError("malformed application scope status")
            if scope.get("grant_status") == 1:
                granted.add(scope["scope_name"])
        report["checked"] = True
        report["granted"] = sorted(granted)
        for feature in features:
            options = FEATURE_SCOPES[feature]
            if not granted.intersection(options):
                report["missing"][feature] = list(options)
        # Request the first (narrowest) option of each missing feature, once.
        requested = []
        for options in report["missing"].values():
            if options[0] not in requested:
                requested.append(options[0])
        report["missing_scopes"] = requested
        report["lifecycle_ready"] = not any(feature in report["missing"] for feature in LIFECYCLE_FEATURES + ("contact_groups", "tenant_name"))
        if requested:
            report["grant_url"] = self.grant_url(requested)
        return report

    def tenant(self):
        result = self.http.request("GET", self.config["base_url"], "/open-apis/tenant/v2/tenant/query",
                                   headers={"Authorization": "Bearer " + self.token})
        if result.get("code") != 0:
            raise SyncError("Feishu tenant verification failed; check tenant:tenant:readonly")
        tenant = result.get("data", {}).get("tenant", {})
        if not isinstance(tenant, dict) or not isinstance(tenant.get("name"), str) or not tenant["name"]:
            raise SyncError("Feishu tenant response has no company name")
        return tenant["name"]

    def snapshot(self, audit=False):
        self.authenticate()
        tenant_name = None
        if self.config.get("expected_tenant_name"):
            tenant_name = self.tenant()
            if tenant_name != self.config["expected_tenant_name"]:
                raise SyncError("Feishu company name does not match expected_tenant_name")
        scope = self.scope()
        departments, users, memberships = {}, {}, {}
        roots = self.config["root_department_ids"]
        for root in sorted(set(roots)):
            if root == "0":
                departments[root] = {"open_department_id": "0", "name": "Feishu root", "parent_department_id": ""}
            else:
                department = self.data("departments/" + quote(root, safe=""), {"department_id_type": "open_department_id"}).get("department")
                if not isinstance(department, dict) or department.get("open_department_id") != root:
                    raise SyncError("configured department is not readable")
                departments[root] = dict(department)
        # A recursive response may omit parent_department_id under the app's
        # field permissions. Immediate-child responses establish each edge from
        # the requested parent without asking for any additional permission.
        parents = {}
        pending = list(departments)
        queued = set(pending)
        for parent in pending:
            for department in self.items("departments/" + quote(parent, safe="") + "/children", query={"fetch_child": "false", "department_id_type": "open_department_id", "user_id_type": "open_id"}, allow_empty_terminal=True):
                key = identifier(department.get("open_department_id"), "department ID")
                if key == parent or key == "0":
                    raise SyncError("cyclic department hierarchy")
                if "parent_department_id" in department and department["parent_department_id"] != parent:
                    raise SyncError("department parent conflicts with direct-child response")
                if key in parents and parents[key] != parent:
                    raise SyncError("department has conflicting direct parents")
                normalized = {**department, "parent_department_id": parent}
                old = departments.get(key, {})
                if any(old[field] != value for field, value in normalized.items() if field in old):
                    raise SyncError("inconsistent duplicate department")
                departments[key] = {**old, **normalized}
                parents[key] = parent
                if key not in queued:
                    queued.add(key)
                    pending.append(key)
        # Only observed direct-child edges extend ancestry. A standalone root's
        # external parent stays outside the selected scope. Overlapping roots
        # share all observed ancestors and are enumerated only once.
        ancestors = {}
        for key in departments:
            path, current = set(), key
            while True:
                if current in path or current not in departments:
                    raise SyncError("cyclic or incomplete department hierarchy")
                path.add(current)
                if current not in parents:
                    if current not in roots:
                        raise SyncError("incomplete department hierarchy")
                    break
                current = parents[current]
            ancestors[key] = path - {"0"}
        for department_id in sorted(departments):
            for user in self.items("users/find_by_department", query={"department_id": department_id, "department_id_type": "open_department_id", "user_id_type": "open_id"}, allow_empty_terminal=True):
                key = identifier(user.get("open_id"), "user open_id")
                status = user.get("status")
                required = ("is_activated", "is_frozen", "is_resigned", "is_exited")
                complete_status = isinstance(status, dict) and all(type(status.get(field)) is bool for field in required)
                if not complete_status and not audit:
                    raise SyncError("user status is incomplete; verify Contact field permissions")
                normalized = {field: status[field] for field in required} if complete_status else None
                if key in users and users[key]["status"] != normalized:
                    raise SyncError("inconsistent duplicate user status")
                try:
                    profile = merge_profiles(users.get(key, {}).get("profile", {}), normalize_profile(user))
                except ProfileError as error:
                    raise SyncError(str(error)) from None
                direct = set(users.get(key, {}).get("department_ids", []))
                if department_id != "0":
                    direct.add(department_id)
                users[key] = {"open_id": key, "status": normalized, "profile": profile, "department_ids": sorted(direct)}
                memberships.setdefault(key, set()).update("feishu-department-" + item for item in ancestors[department_id])
        all_groups = {}
        for group in self.items("group/simplelist", key="grouplist") if self.config["group_ids"] else []:
            key = identifier(group.get("id"), "Contact group ID")
            if key in all_groups and all_groups[key] != group:
                raise SyncError("inconsistent duplicate Contact group")
            all_groups[key] = group
        groups, group_members = {}, {}
        skipped_members = 0
        for group_id in self.config["group_ids"]:
            group = all_groups.get(group_id)
            if group is None:
                raise SyncError("configured Contact group is missing or not authorized")
            if type(group.get("member_department_count")) is not int or group["member_department_count"] < 0:
                raise SyncError("Contact group department membership count is missing")
            groups[group_id] = group
            group_members[group_id] = set()
            for member in self.items("group/" + quote(group_id, safe="") + "/member/simplelist", key="memberlist", query={"member_type": "user", "member_id_type": "open_id"}):
                if member.get("member_type", "user") != "user" or member.get("member_id_type", "open_id") != "open_id":
                    raise SyncError("unsupported Contact group member identity")
                key = identifier(member.get("member_id"), "Contact member open_id")
                if key not in users:
                    skipped_members += 1
                    continue
                group_members[group_id].add(key)
                memberships[key].add("feishu-group-" + group_id)
            if group["member_department_count"]:
                # Request explicit department IDs; do not infer membership from names.
                # Feishu's member_id_type=open_id also denotes an open_department_id
                # when member_type=department (official Contact SDK enum contract).
                for member in self.items("group/" + quote(group_id, safe="") + "/member/simplelist", key="memberlist", query={"member_type": "department", "member_id_type": "open_id"}):
                    if member.get("member_type") != "department" or member.get("member_id_type") != "open_id":
                        raise SyncError("unsupported Contact department-member identity")
                    department_id = identifier(member.get("member_id"), "Contact member department")
                    if department_id not in departments or department_id == "0":
                        raise SyncError("Contact group department lies outside the fully read roots")
                    for key, user_memberships in memberships.items():
                        if "feishu-department-" + department_id in user_memberships:
                            group_members[group_id].add(key)
                            user_memberships.add("feishu-group-" + group_id)
        if self.scope() != scope:
            raise SyncError("Contact permission scope changed during this read")
        return {"scope": scope, "departments": departments, "users": users, "memberships": memberships, "groups": groups, "group_members": group_members, "skipped_group_members": skipped_members, "audit_only": audit, "tenant_name": tenant_name}


class Casdoor:
    def __init__(self, config, http):
        self.config, self.http = config, http
        value = config["client_id"] + ":" + secret(config["client_secret_file"])
        self.headers = {"Authorization": "Basic " + base64.b64encode(value.encode()).decode()}

    def call(self, action, query=None, body=None, mutate=False):
        response = self.http.request("POST" if body is not None else "GET", self.config["base_url"], "/api/" + action, query=query, body=body, headers=self.headers, retry=not mutate)
        if response.get("status") != "ok":
            raise SyncError(f"Casdoor {action} rejected; check API permissions and server logs")
        if mutate and action != "run-syncer" and response.get("data") != "Affected":
            raise SyncError(f"Casdoor {action} did not confirm a write")
        return response.get("data")

    def verify_native(self, feishu_config):
        syncer = self.call("get-syncer", {"id": self.config["native_syncer_id"], "organization": self.config["organization"]})
        if not isinstance(syncer, dict) or syncer.get("type") != "Lark" or syncer.get("organization") != self.config["organization"]:
            raise SyncError("native Lark syncer is absent or belongs to another organization")
        if syncer.get("isEnabled") is not False:
            raise SyncError("disable native syncer scheduling; worker must be the only scheduler")
        if syncer.get("isReadOnly") is not True:
            raise SyncError("native Lark syncer must have isReadOnly=true; Feishu is the source")
        if syncer.get("host", "").rstrip("/") != feishu_config["base_url"] or syncer.get("user") != feishu_config["app_id"]:
            raise SyncError("native syncer host and app ID must match the worker's Feishu source")
        columns = syncer.get("tableColumns")
        if not isinstance(columns, list) or not columns:
            raise SyncError("native syncer must have explicit profile-only tableColumns")
        key_columns = [column for column in columns if column.get("isKey") is True]
        if len(key_columns) != 1 or key_columns[0].get("casdoorName") != "Lark":
            raise SyncError("native syncer must use exactly one immutable Lark binding key")
        allowed_columns = {"Lark", "DisplayName", "Email", "Avatar", "Title", "Phone", "CountryCode", "Gender", "Address"}
        for column in columns:
            name = column.get("casdoorName", "")
            if name not in allowed_columns:
                raise SyncError("native syncer tableColumns overlap worker or immutable identity fields")
            if name == "Lark" and column.get("isHashed") is not False:
                raise SyncError("native Lark key must have isHashed=false")
            if name != "Lark" and column.get("isHashed") is not True:
                raise SyncError("native profile columns must set isHashed=true")
            if not name or column.get("name") != name:
                raise SyncError("native syncer tableColumns must use matching Casdoor-cased name and casdoorName")

        return {column["casdoorName"] for column in columns}

    def import_profiles(self):
        self.call("run-syncer", {"id": self.config["native_syncer_id"], "organization": self.config["organization"]}, mutate=True)

    def snapshot(self):
        query = {"owner": self.config["organization"]}
        # No p/pageSize means the Casdoor endpoint returns the complete organization.
        users, groups = self.call("get-users", query), self.call("get-groups", query)
        if not isinstance(users, list) or not isinstance(groups, list):
            raise SyncError("Casdoor full users/groups response is malformed")
        return users, groups


def source_binding(config):
    fs, cs = config["feishu"], config["casdoor"]
    binding = {"feishu_origin": fs["base_url"], "app_id": fs["app_id"], "tenant_key": fs["tenant_key"], "roots": fs["root_department_ids"], "groups": fs["group_ids"], "casdoor_origin": cs["base_url"], "organization": cs["organization"], "allow_group_id": config["allow_group_id"], "allowed_department_ids": config["allowed_department_ids"], "admission_group": config["admission_group"]}
    # Preserve existing state hashes when company verification is not configured.
    if fs.get("expected_tenant_name"):
        binding["expected_tenant_name"] = fs["expected_tenant_name"]
    return digest(binding)


def load_state(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise SyncError("worker state is unreadable; restore it before applying") from None
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("missing"), dict):
        raise SyncError("worker state schema is invalid")
    return data


def check_state(config, snapshot, state):
    if state and state.get("source_binding") != source_binding(config):
        raise SyncError("source binding changed; review and rebaseline state before applying")
    scope_hash = digest(snapshot["scope"])
    if state and state.get("scope_hash") != scope_hash:
        raise SyncError("Contact permission scope changed; review and rebaseline state before applying")
    return scope_hash


def plan(config, snapshot, users, groups, state):
    if snapshot.get("audit_only"):
        raise SyncError("An audit-only snapshot cannot be applied")
    organization = config["casdoor"]["organization"]
    scope_hash = check_state(config, snapshot, state)
    admission = config["admission_group"]
    if any(item not in snapshot["departments"] for item in config["allowed_department_ids"]):
        raise SyncError("admission department was not included in the complete source read")
    def qualified(name):
        return organization + "/" + name
    def managed(group):
        return group == qualified(admission) or any(group.startswith(qualified(prefix)) for prefix in PREFIXES)
    existing_groups = {}
    for group in groups:
        if group.get("owner") != organization or not isinstance(group.get("name"), str) or group["name"] in existing_groups:
            raise SyncError("Casdoor group ownership or identity is inconsistent")
        existing_groups[group["name"]] = group
    desired_groups = {}
    for key, department in snapshot["departments"].items():
        if key == "0":
            continue
        parent = department.get("parent_department_id")
        parent_name = "feishu-department-" + parent if parent in snapshot["departments"] and parent != "0" and key not in config["feishu"]["root_department_ids"] else organization
        # Casdoor restricts physical groups to one membership. Virtual groups
        # preserve multiple Feishu departments and their ancestor memberships.
        desired_groups["feishu-department-" + key] = {"displayName": str(department.get("name") or key)[:100], "type": "Virtual", "parentId": parent_name, "isTopGroup": parent_name == organization}
    for key, group in snapshot["groups"].items():
        desired_groups["feishu-group-" + key] = {"displayName": str(group.get("name") or key)[:100], "type": "Virtual", "parentId": organization, "isTopGroup": True}
    desired_groups[admission] = {"displayName": "Tailnet members", "type": "Virtual", "parentId": organization, "isTopGroup": True}
    operations = []
    for name, fields in desired_groups.items():
        old = existing_groups.get(name)
        if old and (old.get("properties") or {}).get(OWNER_MARKER) != WORKER_OWNER:
            raise SyncError("managed group name collides with a group not owned by this worker")
        body = copy.deepcopy(old) if old else {"owner": organization, "name": name, "createdTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        body.update(fields)
        body["isEnabled"] = True
        body["properties"] = {**(body.get("properties") or {}), OWNER_MARKER: WORKER_OWNER}
        if not old or any(old.get(field) != body.get(field) for field in (*fields, "isEnabled", "properties")):
            operations.append({"action": "update-group" if old else "add-group", "id": qualified(name), "body": body})
    # Parent groups must exist before children, regardless of API response ordering.
    group_depth = {}
    def depth(name):
        if name not in group_depth:
            parent = desired_groups[name]["parentId"]
            group_depth[name] = 0 if parent == organization else 1 + depth(parent)
        return group_depth[name]
    operations.sort(key=lambda op: (depth(op["body"]["name"]), op["id"]))
    linked, seen_ids = {}, set()
    for user in users:
        if user.get("owner") != organization:
            raise SyncError("Casdoor returned users from another organization")
        if not user.get("lark"):
            continue
        key = identifier(user["lark"], "Casdoor lark binding")
        if key in linked or not user.get("id") or "/" in user["id"] or user["id"] in seen_ids:
            raise SyncError("duplicate or invalid Casdoor identity; resolve linking before sync")
        if not isinstance(user.get("name"), str) or not user["name"] or "/" in user["name"]:
            raise SyncError("invalid Casdoor user API identity")
        linked[key] = user
        seen_ids.add(user["id"])
    known = dict(state.get("known_users", {}))
    missing, blocked, pending, reenables = {}, 0, 0, 0
    for key, user in sorted(linked.items()):
        current_groups = user.get("groups") or []
        if not isinstance(current_groups, list) or any(not isinstance(item, str) for item in current_groups):
            raise SyncError("Casdoor user groups are malformed")
        properties = dict(user.get("properties") or {})
        current_forbidden = user.get("isForbidden", False)
        if type(current_forbidden) is not bool:
            raise SyncError("Casdoor forbidden state must be boolean")
        next_forbidden, reason = current_forbidden, None
        source = snapshot["users"].get(key)
        if key in known and known[key] != user["id"]:
            raise SyncError("previously synchronized Casdoor subject changed")
        if source is None:
            if key not in known:
                # Never offboard unrelated Casdoor Lark accounts outside our scope.
                continue
            prior = state.get("missing", {}).get(key, 0)
            if type(prior) is not int or prior < 0:
                raise SyncError("invalid missing-user state counter")
            missing[key] = prior + 1
            if missing[key] >= config["missing_confirmations"]:
                reason = "missing"
            else:
                pending += 1
                # Retain all previous access during the first missing snapshot.
                continue
        else:
            known[key] = user["id"]
            status = source["status"]
            if not status["is_activated"] or status["is_frozen"] or status["is_resigned"] or status["is_exited"]:
                reason = "inactive"
        if properties.get(HOLD_MARKER) == "true":
            next_forbidden = True
        elif reason:
            if not current_forbidden or properties.get(FORBIDDEN_MARKER):
                properties[FORBIDDEN_MARKER] = reason
            next_forbidden = True
        elif properties.get(FORBIDDEN_MARKER) and config["allow_reenable"]:
            # update-user merges object keys into the old nonnil Properties map.
            # An omitted key is preserved by Go's JSON decoder, so clear the
            # marker with an explicit falsey value instead of omitting it.
            properties[FORBIDDEN_MARKER] = ""
            next_forbidden = False
        target_groups = {item for item in current_groups if not managed(item)}
        target_role = "member"
        if source is not None and not next_forbidden:
            target_groups.update(qualified(item) for item in snapshot["memberships"].get(key, set()))
            admitted = bool(config["allow_group_id"] and key in snapshot["group_members"][config["allow_group_id"]]) or any("feishu-department-" + item in snapshot["memberships"].get(key, set()) for item in config["allowed_department_ids"])
            if admitted:
                target_groups.add(qualified(admission))
                roles = {role for group_id, role in config["headplane_role_groups"].items() if key in snapshot["group_members"].get(group_id, set())}
                target_role = next((role for role in ROLE_PRIORITY if role in roles), "member")
        properties["headplane_role"] = target_role
        if source is not None and config.get("employee_profile", {}).get("enabled"):
            try:
                properties.update(profile_property_delta(source.get("profile", {}), properties,
                    direct_department_ids=source.get("department_ids", []), departments=snapshot["departments"],
                    root_department_ids=config["feishu"]["root_department_ids"],
                    job_levels=snapshot.get("job_levels"), job_families=snapshot.get("job_families")))
            except ProfileError as error:
                raise SyncError(str(error)) from None
            properties["feishu_profile_available_fields"] = json.dumps(sorted(source.get("profile", {})), separators=(",", ":"))
            if snapshot.get("tenant_name"):
                properties["feishu_tenant_name"] = snapshot["tenant_name"]
        if len(json.dumps(sorted(target_groups), separators=(",", ":")).encode()) > 1000:
            raise SyncError("group claim exceeds the 1000-byte compatibility budget")
        updates = {}
        if sorted(set(current_groups)) != sorted(target_groups):
            updates["groups"] = sorted(target_groups)
        old_properties = user.get("properties") or {}
        changed_properties = {key: value for key, value in properties.items()
                              if owned_property(key) and old_properties.get(key) != value}
        if changed_properties:
            updates["properties"] = changed_properties
        if next_forbidden != current_forbidden:
            updates["isForbidden"] = next_forbidden
            blocked += int(next_forbidden)
            reenables += int(not next_forbidden)
        if updates:
            operations.append({"action": "update-user", "id": qualified(user["name"]), "expected_id": user["id"], "expected_lark": key, "expected_mutable": mutable_digest(user), "body": updates})
    next_state = {"version": 1, "source_binding": source_binding(config), "scope_hash": scope_hash, "missing": missing, "known_users": known, "last_success": int(time.time())}
    report = {"source_users": len(snapshot["users"]), "departments": len(snapshot["departments"]) - int("0" in snapshot["departments"]), "contact_groups": len(snapshot["groups"]), "unlinked_source_users": len(set(snapshot["users"]) - set(linked)), "skipped_group_members": snapshot.get("skipped_group_members", 0), "pending_missing_users": pending, "users_blocked": blocked, "users_reenabled": reenables, "operations": len(operations), "scope_hash": scope_hash}
    return operations, next_state, report


def owned_property(key):
    return key == "headplane_role" or key.startswith("feishu_")


def mutable_digest(user):
    properties = {key: value for key, value in (user.get("properties") or {}).items() if owned_property(key)}
    return digest({"groups": sorted(user.get("groups") or []), "properties": properties, "isForbidden": user.get("isForbidden", False)})


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_heartbeat(config, report):
    """Record the last successful interval for container health checks (any mode)."""
    save_state(config["state_file"] + ".heartbeat.json", {"completed_at": int(time.time()), "mode": report.get("mode"), "lifecycle": report.get("lifecycle", "synchronized")})


def load_employee_catalogs(feishu, snapshot, config):
    if config.get("employee_profile", {}).get("enabled") and config["employee_profile"].get("catalog_lookup"):
        for catalog in ("job_levels", "job_families"):
            records = feishu.items(catalog)
            names = {}
            for record in records:
                key = identifier(record.get("id"), catalog + " ID")
                name = record.get("name")
                if not isinstance(name, str) or key in names:
                    raise SyncError("Invalid or duplicate employee catalog record")
                names[key] = name
            snapshot[catalog] = names


def profile_operations(config, snapshot, users):
    """Plan descriptive property updates for linked users; never groups/roles/status."""
    operations, seen = [], set()
    for user in users:
        if user.get("owner") != config["casdoor"]["organization"]:
            raise SyncError("Casdoor returned users from another organization")
        key = user.get("lark")
        if not key:
            continue
        if key in seen:
            raise SyncError("Duplicate Casdoor Feishu binding")
        seen.add(key)
        source = snapshot["users"].get(key)
        if source is None:
            continue
        identifier(user.get("id"), "Casdoor subject")
        identifier(user.get("name"), "Casdoor username")
        try:
            delta = profile_property_delta(source["profile"], user.get("properties") or {},
                direct_department_ids=source["department_ids"], departments=snapshot["departments"],
                root_department_ids=config["feishu"]["root_department_ids"],
                job_levels=snapshot.get("job_levels"), job_families=snapshot.get("job_families"))
        except ProfileError as error:
            raise SyncError(str(error)) from None
        available = json.dumps(sorted(source["profile"]), separators=(",", ":"))
        if (user.get("properties") or {}).get("feishu_profile_available_fields") != available:
            delta["feishu_profile_available_fields"] = available
        if snapshot.get("tenant_name") and (user.get("properties") or {}).get("feishu_tenant_name") != snapshot["tenant_name"]:
            delta["feishu_tenant_name"] = snapshot["tenant_name"]
        if delta:
            operations.append((user, delta))
    return operations, len(set(snapshot["users"]) & seen)


def apply_profile_operations(casdoor, operations):
    for user, delta in operations:
        user_id = user["owner"] + "/" + user["name"]
        latest = casdoor.call("get-user", {"id": user_id})
        if not isinstance(latest, dict) or latest.get("id") != user["id"] or latest.get("lark") != user["lark"] or mutable_digest(latest) != mutable_digest(user):
            raise SyncError("Casdoor identity or owned properties changed during profile update")
        casdoor.call("update-user", {"id": user_id, "columns": "properties"}, {"properties": delta}, mutate=True)


def profile_only(config, apply=False, http=None):
    """Update descriptive properties only; no native imports or access/lifecycle writes."""
    if not config.get("employee_profile", {}).get("enabled"):
        raise SyncError("Enable employee_profile before profile-only synchronization")
    http = http or Http(config["http_timeout_seconds"], config["http_attempts"])
    feishu = Feishu(config["feishu"], http)
    snapshot = feishu.snapshot(audit=True)
    load_employee_catalogs(feishu, snapshot, config)
    # Keep the same source/scope boundary without advancing lifecycle counters.
    check_state(config, snapshot, load_state(config["state_file"]))
    casdoor = Casdoor(config["casdoor"], http)
    users, _ = casdoor.snapshot()
    operations, matched = profile_operations(config, snapshot, users)
    if apply:
        apply_profile_operations(casdoor, operations)
    return {"status": "ok", "mode": "profile-only-apply" if apply else "profile-only-dry-run",
            "source_users": len(snapshot["users"]), "matched_users": matched,
            "updates": len(operations), "profile_coverage": profile_coverage([user["profile"] for user in snapshot["users"].values()])}


def guard_native_columns(native_columns, users, snapshot):
    """Refuse native imports that would erase populated profile data because a source field is unreadable."""
    guards = {"Phone": ("phone", ("mobile",)), "Title": ("title", ("job_title",)), "Email": ("email", ("email", "enterprise_email"))}
    for user in users:
        source = snapshot["users"].get(user.get("lark"))
        if source is None:
            continue
        profile = source.get("profile", {})
        for column, (target, fields) in guards.items():
            # A nonempty observed value can supply the native fallback. An empty
            # result is safe only when every contributing field was observed.
            known_result = any(profile.get(field) for field in fields) or all(field in profile for field in fields)
            if column in native_columns and user.get(target) and not known_result:
                raise SyncError("Native " + column + " source field is unavailable; refusing to erase existing profile data")


def status_gap(config, feishu, snapshot):
    """Describe missing employment status with the scopes an operator must grant."""
    incomplete = sum(user["status"] is None for user in snapshot["users"].values())
    if not incomplete:
        return None
    permissions = feishu.permissions(config)
    scopes = permissions["missing"].get("status") or list(FEATURE_SCOPES["status"])
    return {"users_without_status": incomplete, "missing_scopes": permissions["missing_scopes"] or [scopes[0]],
            "grant_url": permissions.get("grant_url") or feishu.grant_url([scopes[0]]),
            "permissions_checked": permissions["checked"]}


def staged_run(config, feishu, snapshot, state, gap, apply=False, http=None):
    """Populate Casdoor users and descriptive profiles while lifecycle status is unavailable.

    No group, role, forbidden-state or missing-counter changes happen here; the
    next run upgrades itself to full lifecycle synchronization automatically
    once every source user carries a complete status object.
    """
    load_employee_catalogs(feishu, snapshot, config)
    check_state(config, snapshot, state)
    casdoor = Casdoor(config["casdoor"], http)
    native_columns = casdoor.verify_native(config["feishu"])
    users, _ = casdoor.snapshot()
    guard_native_columns(native_columns, users, snapshot)
    if apply:
        casdoor.import_profiles()
        users, _ = casdoor.snapshot()
    operations, matched = ([], len({user.get("lark") for user in users} & set(snapshot["users"])))
    if config.get("employee_profile", {}).get("enabled"):
        operations, matched = profile_operations(config, snapshot, users)
    revocation = {"configured": bool(config.get("headscale")), "pending": len(state.get("pending_revocations", []))}
    if apply:
        apply_profile_operations(casdoor, operations)
        if state and state.get("pending_revocations") and config.get("headscale"):
            retried, pending = revoke_subjects(Tailnet(config["headscale"], http), state["pending_revocations"], (SyncError, TailnetError))
            revocation.update(retried, pending=len(pending))
            save_state(config["state_file"], {**state, "pending_revocations": pending})
    return {"status": "ok", "mode": "apply-staged" if apply else "dry-run-staged", "lifecycle": "skipped",
            "lifecycle_reason": "user status unavailable; admission, roles and offboarding are not synchronized", **gap,
            "source_users": len(snapshot["users"]), "unlinked_source_users": len(set(snapshot["users"]) - {user.get("lark") for user in users}),
            "matched_users": matched, "native_import": apply, "profile_updates": len(operations), "revocation": revocation,
            "profile_coverage": profile_coverage([user["profile"] for user in snapshot["users"].values()]), "completed_at": int(time.time())}


def check_permissions(config, http=None):
    """One-shot scope report without reading the directory."""
    feishu = Feishu(config["feishu"], http or Http(config["http_timeout_seconds"], config["http_attempts"]))
    feishu.authenticate()
    return {"status": "ok", "mode": "permissions", "lifecycle_mode": config["lifecycle_mode"], "permissions": feishu.permissions(config)}


def audit_directory(config, http=None):
    feishu = Feishu(config["feishu"], http or Http(config["http_timeout_seconds"], config["http_attempts"]))
    snapshot = feishu.snapshot(audit=True)
    return {"status": "ok", "mode": "audit", "source_users": len(snapshot["users"]), "permissions": feishu.permissions(config),
            "departments": len(snapshot["departments"]) - int("0" in snapshot["departments"]),
            "direct_memberships": sum(len(user["department_ids"]) for user in snapshot["users"].values()),
            "tenant_name": snapshot.get("tenant_name"), "tenant_verified": bool(snapshot.get("tenant_name")),
            "users_with_complete_status": sum(user["status"] is not None for user in snapshot["users"].values()),
            "profile_coverage": profile_coverage([user["profile"] for user in snapshot["users"].values()])}


def run(config, apply=False, http=None):
    http = http or Http(config["http_timeout_seconds"], config["http_attempts"])
    state = load_state(config["state_file"])
    feishu = Feishu(config["feishu"], http)
    # Read the complete directory first; decide afterwards whether lifecycle
    # synchronization is permitted by the observed status coverage.
    snapshot = feishu.snapshot(audit=True)
    gap = status_gap(config, feishu, snapshot)
    if gap:
        if config["lifecycle_mode"] != "staged":
            raise SyncError("user status is incomplete for %d users; grant %s (%s) or set lifecycle_mode to staged"
                            % (gap["users_without_status"], ", ".join(gap["missing_scopes"]), gap["grant_url"]))
        return staged_run(config, feishu, snapshot, state, gap, apply, http)
    snapshot["audit_only"] = False
    load_employee_catalogs(feishu, snapshot, config)
    check_state(config, snapshot, state)
    casdoor = Casdoor(config["casdoor"], http)
    native_columns = casdoor.verify_native(config["feishu"])
    users, groups = casdoor.snapshot()
    # Guard native columns even when descriptive enrichment is disabled.
    guard_native_columns(native_columns, users, snapshot)
    # Reject known target collisions, changed identities and oversized claims before
    # native import (which itself mutates profiles).
    plan(config, snapshot, users, groups, state)
    if apply:
        casdoor.import_profiles()
        users, groups = casdoor.snapshot()
    operations, next_state, report = plan(config, snapshot, users, groups, state)
    revocation = {"configured": bool(config.get("headscale")), "subjects": 0, "pending": 0}
    if apply:
        blocked_subjects = list(state.get("pending_revocations", []))
        for operation in operations:
            query = {"id": operation["id"]}
            if operation["action"] == "update-user":
                # Detect a user relink before applying only the columns we own.
                latest = casdoor.call("get-user", query)
                if not isinstance(latest, dict) or latest.get("id") != operation["expected_id"] or latest.get("lark") != operation["expected_lark"]:
                    raise SyncError("Casdoor identity changed between plan and apply")
                if mutable_digest(latest) != operation["expected_mutable"]:
                    raise SyncError("Casdoor groups/status/properties changed during apply; retry a fresh plan")
                query["columns"] = ",".join(operation["body"])
            casdoor.call(operation["action"], query, operation["body"], mutate=True)
            if operation["action"] == "update-user" and operation["body"].get("isForbidden") is True and operation["expected_id"] not in blocked_subjects:
                blocked_subjects.append(operation["expected_id"])
        pending = blocked_subjects
        if blocked_subjects and config.get("headscale"):
            # Casdoor blocks are applied; revoke the subjects' enrolled devices and keys.
            revoked, pending = revoke_subjects(Tailnet(config["headscale"], http), blocked_subjects, (SyncError, TailnetError))
            revocation.update(revoked)
        revocation.update(subjects=len(blocked_subjects), pending=len(pending))
        next_state["pending_revocations"] = pending if config.get("headscale") else []
        save_state(config["state_file"], next_state)
        if pending and config.get("headscale"):
            raise SyncError("Casdoor blocks were applied but Headscale revocation is pending for %d subject(s); it is retried on the next run" % len(pending))
    return {"status": "ok", "mode": "apply" if apply else "dry-run", "completed_at": int(time.time()), "revocation": revocation, **report}


def offboard(config, target, apply=False, http=None):
    """Immediate containment: block a Casdoor account, drop managed access and revoke Headscale access."""
    http = http or Http(config["http_timeout_seconds"], config["http_attempts"])
    organization = config["casdoor"]["organization"]
    owner, _, name = target.partition("/")
    if owner != organization or not name or "/" in name:
        raise SyncError("offboard target must be organization/name inside the configured organization")
    casdoor = Casdoor(config["casdoor"], http)
    user = casdoor.call("get-user", {"id": target})
    if not isinstance(user, dict) or user.get("owner") != organization or user.get("name") != name:
        raise SyncError("Casdoor user was not found in the configured organization")
    subject = identifier(user.get("id"), "Casdoor subject")
    admission = organization + "/" + config["admission_group"]
    def managed(group):
        return group == admission or any(group.startswith(organization + "/" + prefix) for prefix in PREFIXES)
    current_groups = user.get("groups") or []
    if not isinstance(current_groups, list) or any(not isinstance(item, str) for item in current_groups):
        raise SyncError("Casdoor user groups are malformed")
    updates = {"properties": {HOLD_MARKER: "true", "headplane_role": "member"}}
    if user.get("isForbidden") is not True:
        updates["isForbidden"] = True
    retained = sorted(item for item in current_groups if not managed(item))
    if retained != sorted(set(current_groups)):
        updates["groups"] = retained
    report = {"status": "ok", "mode": "offboard-apply" if apply else "offboard-dry-run", "target": target,
              "casdoor_updates": sorted(updates), "revocation": {"configured": bool(config.get("headscale"))}}
    if apply:
        casdoor.call("update-user", {"id": target, "columns": ",".join(updates)}, updates, mutate=True)
        if config.get("headscale"):
            try:
                report["revocation"].update(Tailnet(config["headscale"], http).revoke(subject))
            except TailnetError as error:
                raise SyncError(str(error)) from None
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write to Casdoor and durable state")
    mode.add_argument("--audit", action="store_true", help="read directory field coverage only; no writes, even if status is unavailable")
    mode.add_argument("--permissions", action="store_true", help="report granted and missing Feishu app scopes; no directory read, no writes")
    parser.add_argument("--profile-only", action="store_true", help="update descriptive employee properties only; never groups/roles/status/native imports")
    parser.add_argument("--offboard", metavar="ORG/NAME", help="block one Casdoor account now and revoke its Headscale nodes/keys (dry run unless --apply)")
    parser.add_argument("--watch", action="store_true", help="repeat until stopped; failures retry on the next interval")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.audit or args.permissions:
            if args.profile_only or args.offboard:
                raise SyncError("Choose audit, permissions, profile-only or offboard mode")
            if args.watch:
                raise SyncError("Audit and permissions checks are one-shot; do not combine them with --watch")
            print(json.dumps(check_permissions(config) if args.permissions else audit_directory(config), ensure_ascii=False, sort_keys=True))
            return 0
        if args.offboard:
            if args.profile_only or args.watch:
                raise SyncError("Offboarding is one-shot; do not combine --offboard with --profile-only or --watch")
            print(json.dumps(offboard(config, args.offboard, args.apply), ensure_ascii=False, sort_keys=True))
            return 0
        lock = None
        if args.apply:
            lock_path = Path(config["state_file"] + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncError("another worker holds the state lock") from None
        while True:
            try:
                report = profile_only(config, args.apply) if args.profile_only else run(config, args.apply)
                print(json.dumps(report, sort_keys=True), flush=True)
                if args.apply:
                    write_heartbeat(config, report)
            except (SyncError, OSError) as error:
                message = str(error) if isinstance(error, SyncError) else "local state I/O failed"
                print(json.dumps({"status": "error", "error": message, "completed_at": int(time.time())}), file=sys.stderr, flush=True)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(config["interval_seconds"])
    except SyncError as error:
        print(json.dumps({"status": "error", "error": str(error)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
