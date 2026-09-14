# Build on Windows, transfer images over SSH, deploy without a registry

**Using Podman instead of Docker Desktop? Follow [the Podman build guide](podman-build.md) for local setup/build/export, then use the shared transfer and server-import steps here.**

This is the preferred initial deployment path for Fysics. You do not need to buy ACR or set up any registry. Windows performs the builds and downloads. The server receives six finished Linux images plus metadata, then starts them with the existing runtime configuration.

The bundle contains **Headscale, Headplane, Casdoor, the Feishu worker, PostgreSQL, and Caddy**. It contains no `.runtime` directory, application credentials, database contents, or TLS private keys. Application configuration and first-time Feishu/Casdoor setup still happen on the server.

## 1. Check the destination architecture

On `sh-network-01`:

```sh
uname -m
```

Use `linux/amd64` for `x86_64`, or `linux/arm64` for `aarch64`. The examples below assume `linux/amd64`. The checked-in `image-inputs.json` currently pins `linux/amd64`. For an ARM server, change that platform on a feature branch, commit it, and use `linux/arm64` for both build and export. The server must use that same committed configuration. An image for the wrong architecture is rejected by the bundle checks.

## 2. Prepare Windows Docker Desktop and WSL

Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) on **Windows first**, launch it from the Start menu, and wait until its engine is running. Installing WSL/Ubuntu alone does not install Docker.

Use Docker Desktop in Linux-container mode, with its WSL 2 engine and Ubuntu integration enabled. Open **Docker Desktop → Settings → Resources → WSL Integration** and enable your Ubuntu distribution. Do not install a second Docker Engine inside that WSL distribution.

If you do not yet have Ubuntu in WSL, run the following in an administrator PowerShell and complete Windows' prompts/restart:

```powershell
wsl --install -d Ubuntu
```

Open Ubuntu from Windows Terminal, or enter it from PowerShell:

```powershell
wsl -d Ubuntu
```

**In Ubuntu/WSL**, verify:

```sh
docker version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

If `docker` is not found, open Docker Desktop → Settings → Resources → WSL Integration, enable this Ubuntu distribution, apply the change, and reopen the Ubuntu terminal. If the Client appears but the Server does not, start Docker Desktop and wait for the engine. Do not proceed to building until `docker version` shows both.

Docker must show a working Server and Linux containers. Configure your Windows proxy in Docker Desktop's proxy settings if image downloads require it. The remote `BUILD_PROXY_URL=http://127.0.0.1:17890` mode is not for this local build. WSL, Docker's VM, and Windows do not always share loopback, so do not assume that arbitrary containers can directly use Windows `127.0.0.1:7890`. Verify both a Docker image pull and package downloads inside a build with your Desktop proxy configuration.

References: [Docker Desktop WSL integration](https://docs.docker.com/desktop/features/wsl/), [Docker Desktop proxy settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/#proxies).

## 3. Get matching source checkouts locally

These commands run in **Ubuntu/WSL**, not in the remote SSH shell. Use a new workspace if these paths already contain unrelated or modified work.

```sh
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
mkdir -p "$HOME/tailscale-open"
cd "$HOME/tailscale-open"
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/headscale.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/headplane.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/casdoor.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/tailscale-feishu-integration.git
cd tailscale-feishu-integration
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-build.txt
python3 scripts/release.py verify-sources ..
```

If using existing clean checkouts, fetch/update the release branches rather than cloning over them. Expected: source-lock verification passes. Keep source code in WSL's Linux filesystem for the build; only copy the final bundle into Windows Downloads.

## 4. Build the four Fysics images on Windows/WSL

From the local integration checkout, activate its Python environment in **each new terminal**:

```sh
. .venv/bin/activate
python3 -c 'import yaml; print("Python dependencies ready")'
docker version
unset BUILD_PROXY_URL
BUILD_PLATFORM=linux/amd64 bash scripts/build-products.sh ..
```

The script explicitly selects the platform, loads the completed images into the local Docker image store, and attaches source-commit labels to the four built images. All downloaded build dependencies stay on the local build machine. The server does not compile anything.

Existing images built before these source labels were introduced need rebuilding before export. Do not manually label arbitrary images to make validation pass. A failed build must be resolved locally before continuing.

## 5. Export all six images into one folder

Still in local WSL:

```sh
python3 scripts/image-bundle.py export \
  --platform linux/amd64 \
  --pull-supporting-images \
  --output /mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.1-01
```

Adjust `/mnt/c/Users/wjw` if your Windows profile is elsewhere. Use a **new output directory** each time; exports refuse to overwrite an existing bundle. The output appears in Windows as:

```text
C:\Users\wjw\Downloads\fysics-bundle-2026.09-rc.1-01\
  images.tar
  manifest.json
```

`--pull-supporting-images` downloads the pinned PostgreSQL and Caddy references from `image-inputs.json` on the **local machine**. Export checks all six platforms/image IDs and the four source labels, runs `docker image save`, and records the archive SHA-256 and source metadata. The Git working tree must be clean so the recorded commit identifies the scripts/worker actually being delivered.

Only export after the build succeeds. To enforce this when pasting both steps, chain them with `&&`:

```sh
BUILD_PLATFORM=linux/amd64 bash scripts/build-products.sh .. &&
python3 scripts/image-bundle.py export \
  --platform linux/amd64 --pull-supporting-images \
  --output /mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.1-01
```

If `python` is not found, use the `python3` commands above. If creating `.venv` reports missing `ensurepip`, install `python3-venv` in Ubuntu and repeat environment creation. Existing source checkouts do not require recloning to fix either prerequisite.

The tar can be large. Ensure free space on both machines for the bundle and loaded image layers. It is a Docker image archive, not a backup of live application state. Docker `save`/`load` preserves image layers/tags; container filesystem `export`/`import` is a different operation and is not used here.

## 6. Transfer the folder from Windows PowerShell

Open a separate **Windows PowerShell** terminal:

```powershell
scp -r "C:\Users\wjw\Downloads\fysics-bundle-2026.09-rc.1-01" "wjw@8.133.246.13:~/"
```

Use your working SSH key/alias if necessary. For example, add `-i "C:\Users\wjw\.ssh\YOUR_SERVER_KEY.pem"`; for a nonstandard SSH port use `scp -P PORT` (uppercase P).

This transfer uses SSH directly and does not need the port-7890 reverse proxy. Copy the **whole folder**, including its manifest. After transfer, the server should have:

```text
/home/wjw/fysics-bundle-2026.09-rc.1-01/images.tar
/home/wjw/fysics-bundle-2026.09-rc.1-01/manifest.json
```

## 7. Prepare only the integration checkout on the server

The server needs Docker Engine/Compose and Python 3.11+, plus this integration repository. It does not need Go, Node, PyYAML for bundle import, or the three product repositories. Existing product checkouts can remain; there is no reason to delete them during deployment.

If the integration checkout already exists:

```sh
cd "$HOME/tailscale-open/tailscale-feishu-integration"
git fetch origin release/feishu-2026.09-rc.1
```

For a fresh server, install the runtime tools using [deployment steps 1–3](deployment.md), then clone only the integration repository. Git checkout still needs GitHub connectivity; the image-import/start path does not need registry access.

Check out the exact integration commit recorded in the bundle (preserve any existing modifications first):

```sh
bundle_commit=$(python3 - <<'PY'
import json, re
from pathlib import Path
p = Path.home() / 'fysics-bundle-2026.09-rc.1-01/manifest.json'
commit = json.loads(p.read_text())['integration_commit']
assert re.fullmatch(r'[0-9a-f]{40}', commit)
print(commit)
PY
)
git switch --detach "$bundle_commit"
```

A detached release commit is intentional for deployment and does not modify `main`. Import requires this exact commit and the matching lock/input-file hashes; a mismatched server checkout should not silently run different deployment logic.

## 8. Render server-specific configuration, then import

For a **new** deployment, create `.runtime/site.json` following the deployment worksheet. Suggested company hosts:

```json
{
  "casdoor_host": "login.fysics.junweiwang.cn",
  "headscale_host": "vpn.fysics.junweiwang.cn",
  "headplane_host": "network.fysics.junweiwang.cn",
  "tailnet_domain": "tail.fysics.junweiwang.cn",
  "organization": "employees",
  "headscale_client_id": "headscale",
  "headplane_client_id": "headplane",
  "admission_group": "tailnet-members"
}
```

Use DNS records you control. Generate configuration **on the server**, never copy local `.runtime` secrets/data to replace a live installation:

```sh
python3 scripts/configure.py --site .runtime/site.json --output .runtime
```

For an existing deployment with working runtime files, skip regeneration unless deliberately changing configuration. The renderer rewrites service settings and preserves image pins/secrets; back up manual edits before using it.

Import the transferred bundle:

```sh
python3 scripts/image-bundle.py import \
  --bundle "$HOME/fysics-bundle-2026.09-rc.1-01" \
  --runtime .runtime
```

Import checks the archive checksum, source binding, and target platform before calling Docker load. It verifies all loaded image IDs/labels before changing the six image references in `.runtime/compose.env`. Existing configuration, credentials, and data are preserved. Images receive deterministic local tags derived from their image IDs, so subsequent local builds using the original mutable tags do not accidentally change this deployment.

Run the separate pre-start check:

```sh
python3 scripts/image-bundle.py check \
  --bundle "$HOME/fysics-bundle-2026.09-rc.1-01" \
  --runtime .runtime
```

The archive checksum detects transfer corruption. It is not a digital signature; use bundles from your trusted build machine and transfer them over authenticated SSH.

## 9. Start services with registry pulls disabled

Define the offline stack command in your **server shell**:

```sh
stack() {
  docker compose --env-file .runtime/compose.env \
    -f deploy/compose.yaml -f deploy/compose.offline.yaml "$@"
}
stack --profile '*' config --quiet
```

The overlay sets `pull_policy: never` for every service. Missing images fail locally. Do not run `stack pull`; the images are already loaded.

For a first deployment, follow [deployment step 7 onward](deployment.md#7-start-casdoor-privately-and-replace-the-initial-password), using the offline `stack` function above throughout:

```sh
stack up -d db casdoor
```

Complete private Casdoor bootstrap, Feishu app settings, native syncer/worker setup, public TLS, Headscale enrollment, and first-owner setup in their documented order. Loading images does not configure OAuth or start every service automatically.

For an existing installation, take a backup and review migrations before an update; then recreate only the services being updated at the planned maintenance time. Avoid bringing up every profile blindly during first-time setup.

The running apps still need their normal connectivity to Feishu, DNS, certificate authorities, and clients. “Offline image delivery” means no remote registry/build downloads, not an air-gapped authentication system.

## 10. Keep the bundle for rollback and release evidence

Retain the bundle and the matching pre-upgrade application backup. Rolling back images after a database migration also requires restoring the corresponding data backup; image files alone cannot reverse migrations.

The current production promotion checklist still requires live Feishu/OIDC, directory lifecycle, access revocation, and restore evidence. Offline releases can provide the bundle archive checksum and per-image content IDs instead of registry digests. See [build/versioning](build-versioning.md) for recording the offline distribution manifest.

The repository tests use mocked Docker commands for bundle error paths and metadata checks. An actual Windows build, transfer, Docker load, and live deployment must still pass on your machines; no such live result is implied by these instructions.
