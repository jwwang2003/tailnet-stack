#!/usr/bin/env python3
"""Read source locks and enforce local build / production promotion conditions."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("headscale", "headplane", "casdoor")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def read_yaml(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected YAML mapping: {path}")
    return value


def check_lock(lock):
    if lock.get("schema_version") != 1:
        raise ValueError("Unsupported source lock schema")
    for component in COMPONENTS:
        entry = lock.get("components", {}).get(component, {})
        for field in ("upstream_commit", "source_commit"):
            if not SHA.fullmatch(str(entry.get(field, ""))):
                raise ValueError(f"{component}.{field} must be a full commit SHA")
        if not isinstance(entry.get("image"), str) or not entry["image"]:
            raise ValueError(f"{component}.image is required")


def git(path, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(path), *arguments], text=True, stderr=subprocess.PIPE
    ).strip()


def verify_sources(lock, workspace):
    check_lock(lock)
    for component in COMPONENTS:
        path = Path(workspace) / component
        if git(path, "branch", "--show-current") in ("main", "master"):
            raise ValueError(f"{component}: use a downstream branch or detached worktree")
        if git(path, "status", "--porcelain", "--untracked-files=normal"):
            raise ValueError(f"{component}: checkout contains uncommitted files")
        if git(path, "rev-parse", "HEAD") != lock["components"][component]["source_commit"]:
            raise ValueError(f"{component}: HEAD does not match source_commit")


def check_offline_images(lock, manifest, lock_bytes, bundle):
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("Offline distribution requires the exported bundle manifest")
    if bundle.get("integration_commit") != manifest.get("integration_commit"):
        raise ValueError("Offline bundle integration commit differs")
    if bundle.get("versions_lock_sha256") != hashlib.sha256(lock_bytes).hexdigest():
        raise ValueError("Offline bundle source lock differs")
    if bundle.get("platform") != manifest.get("platform"):
        raise ValueError("Offline bundle platform differs")
    archive = bundle.get("archive", {})
    checksum = archive.get("sha256", "")
    if archive.get("file") != "images.tar" or not re.fullmatch(r"[0-9a-f]{64}", str(checksum)):
        raise ValueError("Offline bundle archive checksum missing")
    if manifest.get("bundle_sha256") != checksum:
        raise ValueError("Offline release bundle checksum differs")
    images = bundle.get("images", {})
    expected_keys = set(COMPONENTS) | {"sync", "reverse_proxy", "database"}
    if not isinstance(images, dict) or set(images) != expected_keys:
        raise ValueError("Offline bundle must contain all six images")
    for component, image in images.items():
        if not isinstance(image, dict):
            raise ValueError("Invalid offline image record")
        image_id = image.get("id", "")
        if not DIGEST.fullmatch(str(image_id)):
            raise ValueError("Offline image ID is invalid")
        component_name = component.replace("_", "-")
        aliases = tuple(f"offline/{namespace}-{component_name}:sha256-{image_id[7:]}"
                        for namespace in ("tailnet", "feishu"))
        alias = image.get("alias")
        if alias not in aliases or manifest.get("images", {}).get(component) != alias:
            raise ValueError("Offline image alias differs")
        if f"{image.get('os')}/{image.get('architecture')}" != manifest.get("platform"):
            raise ValueError("Offline image platform differs")
        expected_revision = lock["components"][component]["source_commit"] if component in COMPONENTS else manifest["integration_commit"] if component == "sync" else None
        if expected_revision and image.get("revision") != expected_revision:
            raise ValueError("Offline image source revision differs")


def check_promotion(lock, manifest, lock_bytes, bundle=None):
    check_lock(lock)
    if lock.get("release", {}).get("compatibility_verified") is not True:
        raise ValueError("Release compatibility is not verified")
    distribution = manifest.get("distribution", "registry")
    if distribution == "offline":
        check_offline_images(lock, manifest, lock_bytes, bundle)
    elif distribution == "registry":
        for component in COMPONENTS:
            entry = lock["components"][component]
            if not DIGEST.fullmatch(str(entry.get("image_digest", ""))):
                raise ValueError(f"{component}: immutable image digest is missing")
            expected = f"{entry['image'].split('@')[0]}@{entry['image_digest']}"
            if manifest.get("images", {}).get(component) != expected:
                raise ValueError(f"{component}: manifest image differs from source lock")
        for component in ("sync", "reverse_proxy", "database"):
            ref = manifest.get("images", {}).get(component, "")
            if not isinstance(ref, str) or "@" not in ref or not DIGEST.fullmatch(ref.rsplit("@", 1)[1]):
                raise ValueError(f"{component}: immutable manifest image is missing")
    else:
        raise ValueError("Unknown distribution mode")
    if not SHA.fullmatch(str(manifest.get("integration_commit", ""))):
        raise ValueError("Manifest integration_commit is missing")
    if manifest.get("versions_lock_sha256") != hashlib.sha256(lock_bytes).hexdigest():
        raise ValueError("Manifest source lock checksum differs")
    for field in ("release", "created_at_utc", "platform", "configuration_sha256", "toolchains"):
        if not manifest.get(field):
            raise ValueError(f"Manifest {field} is missing")
    for field in ("oauth_and_identity_linking", "directory_lifecycle", "headscale_and_headplane_oidc", "existing_access_revocation", "isolated_restore"):
        result = manifest.get("validation", {}).get(field)
        if not isinstance(result, dict) or result.get("passed") is not True or not result.get("evidence"):
            raise ValueError(f"Validation evidence missing: {field}")
    for component in COMPONENTS:
        if not manifest.get("migrations", {}).get(component):
            raise ValueError(f"Migration assessment missing: {component}")
    backup = manifest.get("backup", {})
    if not backup.get("reference") or not backup.get("restored_successfully_at_utc"):
        raise ValueError("Backup/restore evidence is missing")
    # A first deployment has no prior version; later releases must retain its restore point.
    rollback = manifest.get("rollback", {})
    if rollback.get("prior_release") and (
        rollback.get("prior_images_retained") is not True
        or not rollback.get("pre_migration_backup_reference")
    ):
        raise ValueError("Previous-release rollback artifacts are missing")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "versions.lock.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("field")
    show.add_argument("component", choices=COMPONENTS)
    show.add_argument("field", choices=("source_commit", "upstream_commit", "tag", "image"))
    auxiliary = sub.add_parser("support-image")
    auxiliary.add_argument("component", choices=("sync", "database", "reverse_proxy"))
    sub.add_parser("platform")
    verify = sub.add_parser("verify-sources")
    verify.add_argument("workspace", type=Path)
    promote = sub.add_parser("check-promotion")
    promote.add_argument("manifest", type=Path)
    promote.add_argument("--bundle-manifest", type=Path)
    args = parser.parse_args()
    try:
        lock = read_yaml(args.lock)
        check_lock(lock)
        if args.command in ("support-image", "platform"):
            inputs = json.loads((ROOT / "image-inputs.json").read_text())
            if inputs.get("schema_version") != 1:
                raise ValueError("Unsupported image input schema")
            value = inputs["platform"] if args.command == "platform" else inputs["images"][args.component]
            if not isinstance(value, str) or not value or any(c.isspace() for c in value):
                raise ValueError("Invalid image input value")
            print(value)
        elif args.command == "field":
            print(lock["components"][args.component][args.field])
        elif args.command == "verify-sources":
            verify_sources(lock, args.workspace)
            print("Source checkouts match the release lock and are clean.")
        else:
            check_promotion(lock, read_yaml(args.manifest), args.lock.read_bytes(),
                            json.loads(args.bundle_manifest.read_text()) if args.bundle_manifest else None)
            print("Recorded production promotion conditions passed; no branches or deployment changed.")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        print(f"Release check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
