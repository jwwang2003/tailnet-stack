#!/usr/bin/env python3
"""Log in to Huawei SWR with AK/SK and push the six local Docker images."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

from release import check_lock, read_yaml

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, password=None):
    # Docker needs only the derived password on stdin, not the original keys.
    env = {key: value for key, value in os.environ.items()
           if key not in ("HUAWEI_AK", "HUAWEI_SK")}
    result = subprocess.run(
        ["docker", *args], input=password, text=True, check=True,
        stdout=subprocess.PIPE if args[:2] == ("image", "inspect") else None,
        env=env,
    )
    return result.stdout


def inspect(reference):
    images = json.loads(docker("image", "inspect", reference))
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError("Expected one local Docker image: " + reference)
    return images[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=os.environ.get("SWR_REGION"),
                        help="Huawei region, or SWR_REGION; e.g. cn-east-3")
    parser.add_argument("--organization", default=os.environ.get("SWR_ORG"),
                        help="Existing SWR organization, or SWR_ORG")
    parser.add_argument("--tag", help="Destination tag; defaults to release image tag plus architecture")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the upload plan without Docker access or credentials")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", args.region or ""):
            raise ValueError("Set --region or SWR_REGION to a Huawei region")
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", args.organization or ""):
            raise ValueError("Set --organization or SWR_ORG to an existing SWR organization")
        registry = f"swr.{args.region}.myhuaweicloud.com"
        if os.environ.get("SWR_REGISTRY", registry) != registry:
            raise ValueError("SWR_REGISTRY does not match --region/SWR_REGION; update or unset it")
        lock = read_yaml(ROOT / "versions.lock.yaml")
        check_lock(lock)
        inputs = json.loads((ROOT / "image-inputs.json").read_text())
        platform = inputs.get("platform")
        if inputs.get("schema_version") != 1 or platform not in ("linux/amd64", "linux/arm64"):
            raise ValueError("Invalid platform/schema in image-inputs.json")
        sources = {name: lock["components"][name]["image"]
                   for name in ("headscale", "headplane", "casdoor")}
        sources.update(sync=inputs["images"]["sync"],
                       postgres=inputs["images"]["database"],
                       caddy=inputs["images"]["reverse_proxy"])
        architecture = platform.split("/")[1]
        release_tag = sources["headscale"].rsplit("/", 1)[-1].partition(":")[2]
        tag = args.tag or (release_tag if release_tag.endswith("-" + architecture)
                           else release_tag + "-" + architecture)
        if (not re.fullmatch(r"[\w][\w.-]{0,127}", tag, flags=re.ASCII)
                or ("@" in sources["headscale"] and not args.tag)):
            raise ValueError("Set --tag to a valid Docker tag")
        targets = {name: f"{registry}/{args.organization}/{name}:{tag}" for name in sources}
        for name, source in sources.items():
            if not isinstance(source, str) or not source or source.startswith("-") or re.search(r"\s", source):
                raise ValueError("Invalid source image: " + name)
            print(f"{source} -> {targets[name]} ({platform})", flush=True)
        if args.dry_run:
            return 0
        ak, sk = os.environ.get("HUAWEI_AK"), os.environ.get("HUAWEI_SK")
        if not ak or not sk:
            raise ValueError("Export HUAWEI_AK and HUAWEI_SK before uploading")
        # Check every source before logging in or publishing any image. Tag the
        # inspected image IDs so another build cannot change the selected source.
        images = {}
        for name, source in sources.items():
            image = inspect(source)
            if f"{image.get('Os')}/{image.get('Architecture')}" != platform:
                raise ValueError(f"{source}: local image platform differs from {platform}")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")):
                raise ValueError("Invalid Docker image ID: " + source)
            images[name] = image["Id"]
        output = ROOT / ".runtime" / "swr-digests" / args.region / args.organization / tag
        output.mkdir(parents=True, exist_ok=True)
        password = hmac.new(sk.encode(), ak.encode(), hashlib.sha256).hexdigest()
        docker("login", "--username", f"{args.region}@{ak}", "--password-stdin",
               registry, password=password + "\n")
        for name, target in targets.items():
            docker("tag", images[name], target)
            docker("push", target)
            image = inspect(target)
            prefix = target.rsplit(":", 1)[0] + "@"
            digests = {value[len(prefix):] for value in image.get("RepoDigests", [])
                       if value.startswith(prefix)}
            if image.get("Id") != images[name] or len(digests) != 1:
                raise ValueError("Cannot determine the pushed image digest: " + target)
            digest = digests.pop()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Invalid registry digest: " + target)
            (output / (name + ".txt")).write_text(digest + "\n")
            print("Published " + prefix + digest, flush=True)
        print("All six images uploaded. Registry digests: " + str(output))
        return 0
    except (ValueError, KeyError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        print("SWR upload failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
