"""Pure, descriptive Feishu employee metadata helpers; no API calls or authorization.

Missing/null optional fields are unavailable and preserve existing properties.
An explicit empty string is an observed empty value. Custom attributes are not
captured: adding them requires a separately configured allowlist and adapter.
"""
import json
import re


class ProfileError(ValueError):
    """A diagnostic that names a field, never its employee-specific value."""


STRING_FIELDS = (
    "name", "mobile", "email", "enterprise_email", "job_title",
    "job_level_id", "job_family_id", "leader_user_id",
)
FIELDS = (*STRING_FIELDS, "employee_type", "is_tenant_manager")
PROPERTY_KEYS = {field: "feishu_" + field for field in FIELDS}
PROPERTY_KEYS["leader_user_id"] = "feishu_manager_open_id"
OWNED_PROPERTY_KEYS = frozenset((*PROPERTY_KEYS.values(), "feishu_departments",
                                 "feishu_job_level_name", "feishu_job_family_name"))


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ProfileError("invalid " + field)
    return value


def _text(value, field):
    if not isinstance(value, str):
        raise ProfileError("invalid or oversized " + field)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ProfileError("invalid text encoding in " + field) from None
    if size > 4096:
        raise ProfileError("invalid or oversized " + field)
    return value.strip()


def normalize_profile(user):
    """Return whitelisted source fields, retaining explicit empty/false/zero values."""
    if not isinstance(user, dict):
        raise ProfileError("employee profile must be an object")
    profile = {}
    for field in FIELDS:
        value = user.get(field)
        if value is None:
            continue
        if field in STRING_FIELDS:
            value = _text(value, field)
            if value and field.endswith("_id"):
                _identifier(value, field)
        elif field == "employee_type":
            if type(value) is not int or not 0 <= value <= 2**31 - 1:
                raise ProfileError("invalid employee_type")
        elif type(value) is not bool:
            raise ProfileError("invalid is_tenant_manager")
        profile[field] = value
    return profile


def merge_profiles(previous, incoming):
    """Merge normalized duplicate reads; absence adds no information, conflicts fail."""
    result = normalize_profile(previous)
    for field, value in normalize_profile(incoming).items():
        if field in result and result[field] != value:
            raise ProfileError("inconsistent duplicate employee field: " + field)
        result[field] = value
    return result


def department_metadata(direct_department_ids, departments, root_department_ids=("0",)):
    """Describe direct memberships and root-to-leaf paths within the selected roots.

    Input departments are snapshot dictionaries keyed by open_department_id.
    The synthetic root "0" is omitted. A selected nonzero root terminates its
    path even when its parent is outside the readable snapshot.
    """
    if not isinstance(departments, dict):
        raise ProfileError("department snapshot must be an object")
    if not isinstance(direct_department_ids, (list, tuple, set, frozenset)):
        raise ProfileError("direct department IDs must be a collection")
    if not isinstance(root_department_ids, (list, tuple, set, frozenset)):
        raise ProfileError("root department IDs must be a collection")
    roots = {_identifier(value, "root department ID") for value in root_department_ids}
    if not roots:
        raise ProfileError("root department IDs must not be empty")
    ids = {_identifier(value, "direct department ID") for value in direct_department_ids} - {"0"}
    records = []
    for department_id in sorted(ids):
        path, seen, current = [], set(), department_id
        while current != "0":
            if current in seen:
                raise ProfileError("cyclic department ancestry")
            seen.add(current)
            department = departments.get(current)
            if not isinstance(department, dict):
                raise ProfileError("incomplete department ancestry")
            if department.get("open_department_id", current) != current:
                raise ProfileError("inconsistent department identity")
            name = _text(department.get("name"), "department name")
            if not name:
                raise ProfileError("missing department name")
            path.append({"id": current, "name": name})
            if current in roots:
                break
            current = _identifier(department.get("parent_department_id"), "department parent")
            if current == "0" and "0" not in roots:
                raise ProfileError("department ancestry lies outside selected roots")
        path.reverse()
        records.append({"id": department_id, "name": path[-1]["name"], "path": path})
    return {"feishu_departments": canonical_json(records)}


def profile_property_delta(profile, existing_properties, *, direct_department_ids=None,
                           departments=None, root_department_ids=("0",),
                           job_levels=None, job_families=None):
    """Return changed descriptive property keys only, suitable for Casdoor map merge.

    Catalogs are optional mappings of source IDs to successfully resolved names.
    Unavailable catalogs preserve names only while the corresponding ID remains
    unchanged. A changed or explicitly cleared ID clears a stale display name.
    None for direct_department_ids preserves existing department metadata;
    an explicit empty collection writes an empty JSON array.
    """
    profile = normalize_profile(profile)
    existing = existing_properties if existing_properties is not None else {}
    if not isinstance(existing, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                            for key, value in existing.items()):
        raise ProfileError("Casdoor properties must contain string keys and values")
    desired = {}
    for field, value in profile.items():
        desired[PROPERTY_KEYS[field]] = ("true" if value else "false") if type(value) is bool else str(value)
    for field, catalog in (("job_level_id", job_levels), ("job_family_id", job_families)):
        if catalog is not None and not isinstance(catalog, dict):
            raise ProfileError("job catalog must be an object")
        source_id = profile.get(field)
        name_key = "feishu_" + field.replace("_id", "_name")
        if source_id and catalog is not None and source_id in catalog:
            name = _text(catalog[source_id], field.replace("_id", " name"))
            if not name:
                raise ProfileError("resolved job catalog name must not be empty")
            desired[name_key] = name
        elif field in profile and existing.get(name_key) and (
                not source_id or source_id != existing.get(PROPERTY_KEYS[field])):
            desired[name_key] = ""
    if direct_department_ids is not None:
        desired.update(department_metadata(direct_department_ids, departments, root_department_ids))
    return {key: value for key, value in desired.items() if existing.get(key) != value}


def profile_coverage(profiles):
    """Count availability and populated values without returning employee values or IDs."""
    result = {"users": 0, "fields": {field: {"available": 0, "populated": 0} for field in FIELDS}}
    for profile in profiles:
        result["users"] += 1
        for field, value in normalize_profile(profile).items():
            result["fields"][field]["available"] += 1
            # False and zero are meaningful, populated observations.
            result["fields"][field]["populated"] += int(value != "")
    return result
