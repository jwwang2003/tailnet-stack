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
            except (URLError, TimeoutError, ConnectionError, OSError):
                if attempt == attempts - 1:
                    raise SyncError(f"API transport failure on {path}") from None
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

    def items(self, path, key="items", query=None):
        result = []
        for page in self.pages(path, query):
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

    def snapshot(self):
        self.authenticate()
        scope = self.scope()
        departments, users, memberships = {}, {}, {}
        roots = self.config["root_department_ids"]
        for root in roots:
            if root == "0":
                departments[root] = {"open_department_id": "0", "name": "Feishu root", "parent_department_id": ""}
            else:
                department = self.data("departments/" + quote(root, safe=""), {"department_id_type": "open_department_id"}).get("department")
                if not isinstance(department, dict) or department.get("open_department_id") != root:
                    raise SyncError("configured department is not readable")
                departments[root] = department
            for department in self.items("departments/" + quote(root, safe="") + "/children", query={"fetch_child": "true", "department_id_type": "open_department_id", "user_id_type": "open_id"}):
                key = identifier(department.get("open_department_id"), "department ID")
                if key in departments and departments[key] != department:
                    raise SyncError("inconsistent duplicate department")
                departments[key] = department
        # Every department must have a resolvable acyclic ancestry within selected roots.
        ancestors = {}
        for key in departments:
            path, current = [], key
            while current:
                if current in path or current not in departments:
                    raise SyncError("cyclic or incomplete department hierarchy")
                path.append(current)
                if current in roots:
                    break
                current = departments[current].get("parent_department_id")
                if not isinstance(current, str) or not current:
                    raise SyncError("missing department parent")
            ancestors[key] = set(path) - {"0"}
        for department_id in sorted(departments):
            for user in self.items("users/find_by_department", query={"department_id": department_id, "department_id_type": "open_department_id", "user_id_type": "open_id"}):
                key = identifier(user.get("open_id"), "user open_id")
                status = user.get("status")
                required = ("is_activated", "is_frozen", "is_resigned", "is_exited")
                if not isinstance(status, dict) or any(type(status.get(field)) is not bool for field in required):
                    raise SyncError("user status is incomplete; verify Contact field permissions")
                normalized = {field: status[field] for field in required}
                if key in users and users[key]["status"] != normalized:
                    raise SyncError("inconsistent duplicate user status")
                users[key] = {"open_id": key, "status": normalized}
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
        return {"scope": scope, "departments": departments, "users": users, "memberships": memberships, "groups": groups, "group_members": group_members, "skipped_group_members": skipped_members}


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
            if not name or column.get("name") != name:
                raise SyncError("native syncer tableColumns must use matching Casdoor-cased name and casdoorName")

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
    return digest({"feishu_origin": fs["base_url"], "app_id": fs["app_id"], "tenant_key": fs["tenant_key"], "roots": fs["root_department_ids"], "groups": fs["group_ids"], "casdoor_origin": cs["base_url"], "organization": cs["organization"], "allow_group_id": config["allow_group_id"], "allowed_department_ids": config["allowed_department_ids"], "admission_group": config["admission_group"]})


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
        if len(json.dumps(sorted(target_groups), separators=(",", ":")).encode()) > 1000:
            raise SyncError("group claim exceeds the 1000-byte compatibility budget")
        updates = {}
        if sorted(set(current_groups)) != sorted(target_groups):
            updates["groups"] = sorted(target_groups)
        if properties != (user.get("properties") or {}):
            updates["properties"] = properties
        if next_forbidden != current_forbidden:
            updates["isForbidden"] = next_forbidden
            blocked += int(next_forbidden)
            reenables += int(not next_forbidden)
        if updates:
            operations.append({"action": "update-user", "id": qualified(user["name"]), "expected_id": user["id"], "expected_lark": key, "expected_mutable": mutable_digest(user), "body": updates})
    next_state = {"version": 1, "source_binding": source_binding(config), "scope_hash": scope_hash, "missing": missing, "known_users": known, "last_success": int(time.time())}
    report = {"source_users": len(snapshot["users"]), "departments": len(snapshot["departments"]) - int("0" in snapshot["departments"]), "contact_groups": len(snapshot["groups"]), "unlinked_source_users": len(set(snapshot["users"]) - set(linked)), "skipped_group_members": snapshot.get("skipped_group_members", 0), "pending_missing_users": pending, "users_blocked": blocked, "users_reenabled": reenables, "operations": len(operations), "scope_hash": scope_hash}
    return operations, next_state, report


def mutable_digest(user):
    return digest({"groups": sorted(user.get("groups") or []), "properties": user.get("properties") or {}, "isForbidden": user.get("isForbidden", False)})


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


def run(config, apply=False, http=None):
    http = http or Http(config["http_timeout_seconds"], config["http_attempts"])
    state = load_state(config["state_file"])
    snapshot = Feishu(config["feishu"], http).snapshot()
    check_state(config, snapshot, state)
    casdoor = Casdoor(config["casdoor"], http)
    casdoor.verify_native(config["feishu"])
    users, groups = casdoor.snapshot()
    # Reject known target collisions, changed identities and oversized claims before
    # native import (which itself mutates profiles).
    plan(config, snapshot, users, groups, state)
    if apply:
        casdoor.import_profiles()
        users, groups = casdoor.snapshot()
    operations, next_state, report = plan(config, snapshot, users, groups, state)
    if apply:
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
        save_state(config["state_file"], next_state)
    return {"status": "ok", "mode": "apply" if apply else "dry-run", "completed_at": int(time.time()), **report}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="write to Casdoor and durable state")
    parser.add_argument("--watch", action="store_true", help="repeat until stopped; failures retry on the next interval")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
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
                print(json.dumps(run(config, args.apply), sort_keys=True), flush=True)
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
