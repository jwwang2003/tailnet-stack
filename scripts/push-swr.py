#!/usr/bin/env python
"""Log in to Huawei SWR with AK/SK and push the six local Docker images."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Annotated

import yaml
import typer
from rich.console import Console
from rich.table import Table

from release import check_lock, read_yaml

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(add_completion=False, rich_markup_mode="rich",
                  pretty_exceptions_show_locals=False)
console = Console(markup=False, highlight=False)
errors = Console(stderr=True, markup=False, highlight=False)


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


@app.command()
def main(
    region: Annotated[str, typer.Option(envvar="SWR_REGION", help="Huawei region, e.g. cn-east-3")],
    organization: Annotated[str, typer.Option(envvar="SWR_ORG", help="Existing SWR organization")],
    tag: Annotated[str | None, typer.Option(help="Destination tag; defaults to release tag plus architecture")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview without Docker access or credentials")] = False,
):
    """Log in to Huawei SWR and upload the six local Docker images."""
    try:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region or ""):
            raise ValueError("Set --region or SWR_REGION to a Huawei region")
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", organization or ""):
            raise ValueError("Set --organization or SWR_ORG to an existing SWR organization")
        registry = f"swr.{region}.myhuaweicloud.com"
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
        if "@" in sources["headscale"] and not tag:
            raise ValueError("Set --tag when the source image is pinned by digest")
        tag = tag or (release_tag if release_tag.endswith("-" + architecture)
                           else release_tag + "-" + architecture)
        if not re.fullmatch(r"[\w][\w.-]{0,127}", tag, flags=re.ASCII):
            raise ValueError("Set --tag to a valid Docker tag")
        targets = {name: f"{registry}/{organization}/{name}:{tag}" for name in sources}
        plan = Table(title=f"SWR upload · {platform}", show_lines=True)
        plan.add_column("Image", style="cyan", no_wrap=True)
        plan.add_column("Local source", overflow="fold")
        plan.add_column("Destination", overflow="fold")
        for name, source in sources.items():
            if not isinstance(source, str) or not source or source.startswith("-") or re.search(r"\s", source):
                raise ValueError("Invalid source image: " + name)
            plan.add_row(name, source, targets[name])
        console.print(plan)
        if dry_run:
            console.print("Preview only — no login or upload performed.", style="yellow")
            return
        ak, sk = os.environ.get("HUAWEI_AK"), os.environ.get("HUAWEI_SK")
        if not ak or not sk:
            raise ValueError("Export HUAWEI_AK and HUAWEI_SK before uploading")
        # Check every source before logging in or publishing any image. Tag the
        # inspected image IDs so another build cannot change the selected source.
        console.print("Checking all six local images…", style="cyan")
        images = {}
        for name, source in sources.items():
            image = inspect(source)
            if f"{image.get('Os')}/{image.get('Architecture')}" != platform:
                raise ValueError(f"{source}: local image platform differs from {platform}")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")):
                raise ValueError("Invalid Docker image ID: " + source)
            images[name] = image["Id"]
        output = ROOT / ".runtime" / "swr-digests" / region / organization / tag
        output.mkdir(parents=True, exist_ok=True)
        password = hmac.new(sk.encode(), ak.encode(), hashlib.sha256).hexdigest()
        console.print("Logging in to " + registry, style="cyan")
        docker("login", "--username", f"{region}@{ak}", "--password-stdin",
               registry, password=password + "\n")
        for index, (name, target) in enumerate(targets.items(), start=1):
            console.print(f"[{index}/6] Uploading {name}", style="bold cyan")
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
            console.print("Published " + prefix + digest, style="green", soft_wrap=True)
        console.print("All six images uploaded.", style="bold green")
        console.print("Registry digests: " + str(output), soft_wrap=True)
    except (ValueError, KeyError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        errors.print("SWR upload failed: " + str(error), style="bold red", soft_wrap=True)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
