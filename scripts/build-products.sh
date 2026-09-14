#!/usr/bin/env bash
set -euo pipefail

integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace=${1:-$(dirname -- "$integration_root")}

command -v docker >/dev/null || {
  echo 'Docker CLI was not found in this shell.' >&2
  echo 'Windows/WSL: install/start Docker Desktop in Windows, select Linux containers, and enable Settings > Resources > WSL Integration for this Ubuntu distribution. Reopen the terminal and run docker version.' >&2
  echo 'Native Linux: install Docker Engine with BuildKit. Do not install a second Engine inside WSL when using Docker Desktop.' >&2
  exit 1
}
command -v python3 >/dev/null || { echo 'Python 3 is required; activate the project .venv after installing python3-venv.' >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || {
  echo 'PyYAML is missing. Run: python3 -m venv .venv && . .venv/bin/activate && python3 -m pip install -r requirements-build.txt' >&2
  exit 1
}
# A reverse SSH proxy is reachable on the Linux host, not the default build bridge.
# Opt in only for a local, rootful Linux daemon with its embedded Docker builder.
build_platform=${BUILD_PLATFORM:-$(python3 "$integration_root/scripts/release.py" platform)}
case "$build_platform" in linux/amd64|linux/arm64) ;; *) echo 'BUILD_PLATFORM must be linux/amd64 or linux/arm64.' >&2; exit 1;; esac
build_flags=(--platform "$build_platform" --load)
if [[ -n "${BUILD_PROXY_URL:-}" ]]; then
  if [[ ! "$BUILD_PROXY_URL" =~ ^http://127\.0\.0\.1:([0-9]{1,5})$ ]]; then
    echo 'BUILD_PROXY_URL must be http://127.0.0.1:PORT for the SSH tunnel.' >&2
    exit 1
  fi
  proxy_port=$((10#${BASH_REMATCH[1]}))
  ((proxy_port >= 1 && proxy_port <= 65535)) || { echo 'Invalid proxy port.' >&2; exit 1; }
  docker_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
  [[ "$docker_endpoint" == unix://* ]] || { echo 'Tunnel builds require a local Linux Docker socket.' >&2; exit 1; }
  [[ "$(docker info --format '{{.OSType}}')" == linux ]] || { echo 'Tunnel builds require Linux Docker Engine.' >&2; exit 1; }
  if [[ "$(docker info --format '{{json .SecurityOptions}}')" == *rootless* ]]; then
    echo 'This loopback tunnel recipe requires rootful Docker Engine.' >&2
    exit 1
  fi
  [[ "$(docker buildx inspect default --format '{{.Driver}}')" == docker ]] || {
    echo 'Tunnel builds require the local default Docker builder, not a container/remote builder.' >&2
    exit 1
  }
  export HTTP_PROXY="$BUILD_PROXY_URL" HTTPS_PROXY="$BUILD_PROXY_URL" ALL_PROXY="$BUILD_PROXY_URL"
  export http_proxy="$BUILD_PROXY_URL" https_proxy="$BUILD_PROXY_URL" all_proxy="$BUILD_PROXY_URL"
  export NO_PROXY="${BUILD_NO_PROXY:-localhost,127.0.0.1,::1}" no_proxy="${BUILD_NO_PROXY:-localhost,127.0.0.1,::1}"
  build_flags+=(--builder default --network host)
  for proxy_variable in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
    build_flags+=(--build-arg "$proxy_variable")
  done
  echo 'Build RUN steps will use the SSH proxy through host networking. Registry pulls still require the daemon proxy.'
fi

python3 "$integration_root/scripts/release.py" verify-sources "$workspace"
headscale_image=$(python3 "$integration_root/scripts/release.py" field headscale image)
headplane_image=$(python3 "$integration_root/scripts/release.py" field headplane image)
casdoor_image=$(python3 "$integration_root/scripts/release.py" field casdoor image)
headscale_commit=$(python3 "$integration_root/scripts/release.py" field headscale source_commit)
headplane_commit=$(python3 "$integration_root/scripts/release.py" field headplane source_commit)
casdoor_commit=$(python3 "$integration_root/scripts/release.py" field casdoor source_commit)
sync_image=$(python3 "$integration_root/scripts/release.py" support-image sync)
integration_commit=$(git -C "$integration_root" rev-parse HEAD)

docker build "${build_flags[@]}" --file "$integration_root/build/headscale.Dockerfile" \
  --build-arg "HEADSCALE_VERSION=${HEADSCALE_VERSION:-v0.29.3-feishu.1}" \
  --label "org.opencontainers.image.revision=$headscale_commit" \
  --tag "$headscale_image" "$workspace/headscale"
docker build "${build_flags[@]}" --target final \
  --build-arg "HEADPLANE_VERSION=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --build-arg "IMAGE_TAG=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --label "org.opencontainers.image.revision=$headplane_commit" \
  --tag "$headplane_image" "$workspace/headplane"
docker build "${build_flags[@]}" --target STANDARD \
  --label "org.opencontainers.image.revision=$casdoor_commit" \
  --tag "$casdoor_image" "$workspace/casdoor"
docker build "${build_flags[@]}" --file "$integration_root/build/sync.Dockerfile" \
  --label "org.opencontainers.image.revision=$integration_commit" \
  --tag "$sync_image" "$integration_root"
echo 'Local candidate images built. Export an image bundle for SSH deployment, or publish to a registry. Test before production.'
