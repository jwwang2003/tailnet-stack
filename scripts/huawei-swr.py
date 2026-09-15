#!/usr/bin/env python
"""Log in to Huawei SWR with AK/SK and upload the local Docker images."""

import hashlib
import hmac
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from release import check_lock, read_yaml

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console(markup=False, highlight=False)
errors = Console(stderr=True, markup=False, highlight=False)


def docker(*args: str, password: str | None = None) -> str | None:
    # Pass the derived password on stdin; keep the original keys out of Docker's env.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("HUAWEI_AK", "HUAWEI_SK")
    }
    result = subprocess.run(
        ["docker", *args],
        input=password,
        text=True,
        check=True,
        stdout=subprocess.PIPE if args[:2] == ("image", "inspect") else None,
        env=env,
    )
    return result.stdout


def inspect_image(reference: str) -> dict[str, Any]:
    images = json.loads(docker("image", "inspect", reference))
    if not isinstance(images, list) or len(images) != 1:
        raise ValueError(f"Expected one local Docker image: {reference}")
    return images[0]


def resolve_registry(region: str, organization: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region):
        raise ValueError("Set --region or SWR_REGION to a Huawei region")
    if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", organization):
        raise ValueError("Set --organization or SWR_ORG to an existing SWR organization")

    registry = f"swr.{region}.myhuaweicloud.com"
    if os.environ.get("SWR_REGISTRY", registry) != registry:
        raise ValueError(
            "SWR_REGISTRY does not match --region/SWR_REGION; update or unset it"
        )
    return registry


def load_sources() -> tuple[str, dict[str, str]]:
    """Read the platform and six image references from the release files."""
    lock = read_yaml(ROOT / "versions.lock.yaml")
    check_lock(lock)

    inputs = json.loads((ROOT / "image-inputs.json").read_text())
    platform = inputs.get("platform")
    if inputs.get("schema_version") != 1 or platform not in ("linux/amd64", "linux/arm64"):
        raise ValueError("Invalid platform/schema in image-inputs.json")

    sources = {
        name: lock["components"][name]["image"]
        for name in ("headscale", "headplane", "casdoor")
    }
    sources.update(
        sync=inputs["images"]["sync"],
        postgres=inputs["images"]["database"],
        caddy=inputs["images"]["reverse_proxy"],
    )

    for name, source in sources.items():
        if (
            not isinstance(source, str)
            or not source
            or source.startswith("-")
            or re.search(r"\s", source)
        ):
            raise ValueError(f"Invalid source image: {name}")
    return platform, sources


def resolve_tag(source: str, platform: str, tag: str | None) -> str:
    if not tag:
        # A digest identifies content but provides no release tag to reuse.
        if "@" in source:
            raise ValueError("Set --tag when the source image is pinned by digest")

        tag = source.rsplit("/", 1)[-1].partition(":")[2]
        architecture = platform.split("/")[1]
        if not tag.endswith(f"-{architecture}"):
            tag = f"{tag}-{architecture}"

    if not re.fullmatch(r"[\w][\w.-]{0,127}", tag, flags=re.ASCII):
        raise ValueError("Set --tag to a valid Docker tag")
    return tag


def show_plan(platform: str, sources: dict[str, str], targets: dict[str, str]) -> None:
    table = Table(title=f"SWR upload · {platform}", show_lines=True)
    table.add_column("Image", style="cyan", no_wrap=True)
    table.add_column("Local source", overflow="fold")
    table.add_column("Destination", overflow="fold")

    for name, source in sources.items():
        table.add_row(name, source, targets[name])
    console.print(table)


def check_local_images(sources: dict[str, str], platform: str) -> dict[str, str]:
    """Validate every source before login and retain the inspected image IDs."""
    console.print("Checking all six local images…", style="cyan")
    image_ids = {}

    for name, source in sources.items():
        image = inspect_image(source)
        actual_platform = f"{image.get('Os')}/{image.get('Architecture')}"
        if actual_platform != platform:
            raise ValueError(f"{source}: local image platform differs from {platform}")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")):
            raise ValueError(f"Invalid Docker image ID: {source}")
        image_ids[name] = image["Id"]

    return image_ids


def login(region: str, registry: str, access_key: str, secret_key: str) -> None:
    # SWR's general long-term password is HMAC-SHA256(SK, AK), encoded as hex.
    password = hmac.new(
        secret_key.encode(), access_key.encode(), hashlib.sha256
    ).hexdigest()

    console.print(f"Logging in to {registry}", style="cyan")
    docker(
        "login",
        "--username", f"{region}@{access_key}",
        "--password-stdin", registry,
        password=password + "\n",
    )


def publish_image(image_id: str, target: str) -> str:
    """Push the selected image and return its registry manifest digest."""
    # Use the inspected ID so a concurrent build cannot change the source tag.
    docker("tag", image_id, target)
    docker("push", target)

    image = inspect_image(target)
    repository = target.rsplit(":", 1)[0]
    prefix = f"{repository}@"
    digests = {
        value[len(prefix):]
        for value in image.get("RepoDigests", [])
        if value.startswith(prefix)
    }
    if image.get("Id") != image_id or len(digests) != 1:
        raise ValueError(f"Cannot determine the pushed image digest: {target}")

    digest = digests.pop()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(f"Invalid registry digest: {target}")
    return digest


@app.command()
def main(
    region: Annotated[
        str,
        typer.Option(envvar="SWR_REGION", help="Huawei region, e.g. cn-east-3"),
    ],
    organization: Annotated[
        str,
        typer.Option(envvar="SWR_ORG", help="Existing SWR organization"),
    ],
    tag: Annotated[
        str | None,
        typer.Option(help="Destination tag; defaults to release tag plus architecture"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without Docker access or credentials"),
    ] = False,
) -> None:
    """Log in to Huawei SWR and upload the local Docker images."""
    try:
        registry = resolve_registry(region, organization)
        platform, sources = load_sources()
        tag = resolve_tag(sources["headscale"], platform, tag)
        targets = {
            name: f"{registry}/{organization}/{name}:{tag}"
            for name in sources
        }
        show_plan(platform, sources, targets)

        # Preview ends before reading credentials or calling Docker.
        if dry_run:
            console.print("Preview only — no login or upload performed.", style="yellow")
            return

        access_key = os.environ.get("HUAWEI_AK")
        secret_key = os.environ.get("HUAWEI_SK")
        if not access_key or not secret_key:
            raise ValueError("Export HUAWEI_AK and HUAWEI_SK before uploading")

        image_ids = check_local_images(sources, platform)
        output = ROOT / ".runtime" / "swr-digests" / region / organization / tag
        output.mkdir(parents=True, exist_ok=True)
        login(region, registry, access_key, secret_key)

        for index, (name, target) in enumerate(targets.items(), start=1):
            console.print(f"[{index}/{len(targets)}] Uploading {name}", style="bold cyan")
            digest = publish_image(image_ids[name], target)

            # Save each successful push even if a later upload fails.
            (output / f"{name}.txt").write_text(digest + "\n")
            repository = target.rsplit(":", 1)[0]
            console.print(f"Published {repository}@{digest}", style="green", soft_wrap=True)

        console.print("All six images uploaded.", style="bold green")
        console.print(f"Registry digests: {output}", soft_wrap=True)
    except (ValueError, KeyError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        errors.print(f"SWR upload failed: {error}", style="bold red", soft_wrap=True)
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
