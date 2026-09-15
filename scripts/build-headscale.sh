#!/usr/bin/env bash
set -euo pipefail

integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace=${1:-$(dirname -- "$integration_root")}
output_directory=${2:-"$integration_root/dist/headscale"}
release_version=${HEADSCALE_VERSION:-v0.29.3-integrated.1}

command -v go >/dev/null || { echo 'Install the Go toolchain from headscale/go.mod before building.' >&2; exit 1; }
release_args=()
if [[ -n "${DEPLOYMENT_FILE:-}" ]]; then
  release_args+=(--deployment "$DEPLOYMENT_FILE")
fi
python3 "$integration_root/scripts/release.py" "${release_args[@]}" verify-sources "$workspace"
mkdir -p -- "$output_directory"
output_directory=$(cd -- "$output_directory" && pwd)
cd -- "$workspace/headscale"
go version > "$output_directory/toolchain.txt"
git rev-parse HEAD > "$output_directory/source-commit.txt"
go mod download
go mod verify
go build -trimpath -buildmode=pie -ldflags "-X github.com/juanfont/headscale/hscontrol/types.VersionOverride=$release_version" -o "$output_directory/headscale" ./cmd/headscale
"$output_directory/headscale" version
sha256sum "$output_directory/headscale" > "$output_directory/headscale.sha256"
echo "Built Headscale into $output_directory; run the compatibility checks before promotion."
