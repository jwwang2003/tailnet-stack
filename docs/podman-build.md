# Local rootless Podman build, remote Docker deployment

Use this path when you prefer Podman inside Ubuntu/WSL instead of Docker Desktop. The remote deployment still uses Docker Engine/Compose. No registry subscription is needed.

## 1. Check Podman and rootless mappings

```sh
podman --version
podman unshare cat /proc/self/uid_map
podman unshare cat /proc/self/gid_map
```

On the current `fysics-office-wjw` machine, `wjw` is UID 1000 and now has the subordinate range `165536:65536` in both `/etc/subuid` and `/etc/subgid`. The older `fysics-wjw:100000:65536` entry remains reserved. After `podman system migrate`, the namespace should show:

```text
         0       1000          1
         1     165536      65536
```

If you see only the first line, Podman is still using single-ID mapping. An administrator must assign this user non-overlapping ranges, then run `podman system migrate` as the user. Migration can stop that user's running Podman containers. Do not blindly reuse these numeric ranges on another machine; inspect its existing assignments first.

This prerequisite is already fixed on the current workstation. No Docker Desktop installation or Docker-to-Podman symlink is required. Use the `podman` command directly. The build script also recognizes a `docker` symlink whose real target is Podman.

## 2. Update and activate Python

From the local WSL integration checkout:

```sh
cd ~/workspace/development/tailscale-open/tailnet-stack
git pull --ff-only
source .venv/bin/activate
python3 -c 'import yaml; print("Python ready")'
```

If preparing a fresh workstation, install Podman and Python/venv using Ubuntu's packages and create the Python environment using `requirements-build.txt`. Use the matching release checkouts for the three product repositories, as documented in [local build/deploy](local-build-deploy.md).

## 3. Build with Podman

```sh
unset BUILD_PROXY_URL
CONTAINER_ENGINE=podman BUILD_PLATFORM=linux/amd64 \
  bash scripts/build-products.sh ..
```

The script:

- Checks rootless UID/GID mappings before pulling images.
- Generates temporary build recipes with fully qualified Docker Hub references, e.g. `docker.io/library/golang:1.26.5`. It does not edit `/etc/containers/registries.conf` or the clean product checkouts.
- Supplies the platform arguments required by the pinned recipes.
- Sets `--ulimit nofile=65536:65536` for build steps so Vite can resolve dependencies without exhausting the default Buildah file-descriptor limit.
- Uses native `podman build --format docker --layers`; it does not pass Docker Buildx's `--load` or require a Docker daemon.
- Preserves source-revision labels needed by bundle verification.

The supported local recipe is a native Linux/amd64 build on this workstation. An ARM target also requires a matching committed `image-inputs.json` plus working ARM build execution/emulation; do not assume changing a flag installs emulation.

If an image pull or module download fails, resolve local Podman/WSL network access. Podman is a local process rather than the Docker Desktop VM: proxy environment variables and `BUILD_PROXY_URL` must refer to an endpoint reachable from this WSL environment. Do not reuse remote port 17890 unless you actually have a proxy listening there locally. A registry short-name error is fixed by the qualified recipes; a later network timeout is a separate issue.

## 4. Export in Docker-compatible archive format

Only after the build succeeds:

```sh
python3 scripts/image-bundle.py export --engine podman \
  --platform linux/amd64 \
  --pull-supporting-images \
  --output /mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.4-01
```

Or chain both steps:

```sh
CONTAINER_ENGINE=podman BUILD_PLATFORM=linux/amd64 bash scripts/build-products.sh .. &&
python3 scripts/image-bundle.py export --engine podman \
  --platform linux/amd64 --pull-supporting-images \
  --output /mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.4-01
```

The exporter uses `podman image save --format docker-archive --multi-image-archive` for all six images and normalizes Podman's image ID representation. The supporting PostgreSQL/Caddy pulls also use fully qualified Docker Hub names. Use a new output directory for every bundle.

Do not substitute `podman export` (a container filesystem export), or export each image with an incompatible archive format. The repository helper records checksum, platform, source commits, and IDs for the remote checks.

## 5. Transfer and import on the server

Use the PowerShell `scp` and remote import/check steps in [local build/deploy](local-build-deploy.md#6-transfer-the-folder-from-windows-powershell). Transfer the entire folder. On the remote host, use the bundle's matching integration commit and run:

```sh
python3 scripts/image-bundle.py import \
  --bundle "$HOME/fysics-bundle-2026.09-rc.4-01" --runtime .runtime
python3 scripts/image-bundle.py check \
  --bundle "$HOME/fysics-bundle-2026.09-rc.4-01" --runtime .runtime
```

Remote import/check deliberately uses Docker; `--engine podman` applies only to local export. Keep `deploy/compose.offline.yaml` in server Compose commands so it cannot pull missing images.

## Validation performed

The current workstation passed a real rootless Podman scratch-image build and multi-image Docker archive export. Inspection of the archive confirmed both saved config digests equal their original image IDs. Unit tests cover Podman selection, qualified build recipes, raw-ID normalization, archive flags, and the Podman-export/Docker-import command boundary.

This smoke test does not claim a completed build of all four applications or a live load on the remote Docker daemon; the build and import commands above are still the live acceptance steps.

## Vite reports a dependency missing although it is installed

On this workstation, Casdoor's frontend failed to resolve `html-parse-stringify`
during a Podman build even though the package and its module entry point existed
in the cached image. Running the same frontend build in a normal container passed.
The measured open-file limit was 1024 for Buildah RUN steps versus 1048576 for
normal containers. Rebuilding the FRONT stage with `--ulimit nofile=65536:65536`
passed, including Vite and the postbuild step, without changing dependencies.

The script now sets that limit for Podman builds. Update the integration checkout
and rerun the same build/export command. Do not externalize the missing import or
change the lockfile based solely on that error. An unrelated peer-dependency
warning does not identify the cause of this failure.
