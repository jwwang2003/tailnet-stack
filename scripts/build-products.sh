#!/usr/bin/env bash
set -euo pipefail

integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace=${1:-$(dirname -- "$integration_root")}

command -v docker >/dev/null || { echo 'Install Docker with BuildKit before building images.' >&2; exit 1; }
python3 "$integration_root/scripts/release.py" verify-sources "$workspace"
headscale_image=$(python3 "$integration_root/scripts/release.py" field headscale image)
headplane_image=$(python3 "$integration_root/scripts/release.py" field headplane image)
casdoor_image=$(python3 "$integration_root/scripts/release.py" field casdoor image)
headscale_commit=$(python3 "$integration_root/scripts/release.py" field headscale source_commit)
headplane_commit=$(python3 "$integration_root/scripts/release.py" field headplane source_commit)
casdoor_commit=$(python3 "$integration_root/scripts/release.py" field casdoor source_commit)

docker build --file "$integration_root/build/headscale.Dockerfile" \
  --build-arg "HEADSCALE_VERSION=${HEADSCALE_VERSION:-v0.29.3-feishu.1}" \
  --label "org.opencontainers.image.revision=$headscale_commit" \
  --tag "$headscale_image" "$workspace/headscale"
docker build --target final \
  --build-arg "HEADPLANE_VERSION=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --build-arg "IMAGE_TAG=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --label "org.opencontainers.image.revision=$headplane_commit" \
  --tag "$headplane_image" "$workspace/headplane"
docker build --target STANDARD \
  --label "org.opencontainers.image.revision=$casdoor_commit" \
  --tag "$casdoor_image" "$workspace/casdoor"
echo 'Local candidate images built. Record registry digests and test the complete tuple before production.'
