#!/usr/bin/env bash
set -euo pipefail

integration_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
workspace=${1:-$(dirname -- "$integration_root")}

command -v python3 >/dev/null || { echo 'Python 3 is required.' >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || {
  echo 'Activate .venv and run python3 -m pip install -r requirements-build.txt.' >&2; exit 1;
}
engine=${CONTAINER_ENGINE:-auto}
if [[ "$engine" == auto ]]; then
  if command -v docker >/dev/null; then
    docker_target=$(python3 -c 'import os,shutil; print(os.path.realpath(shutil.which("docker")))')
    if [[ "${docker_target##*/}" == podman* ]]; then engine=podman; else engine=docker; fi
  elif command -v podman >/dev/null; then engine=podman
  else echo 'Install Docker Engine/Desktop or Podman before building.' >&2; exit 1
  fi
fi
case "$engine" in docker|podman) ;; *) echo 'CONTAINER_ENGINE must be auto, docker, or podman.' >&2; exit 1;; esac
command -v "$engine" >/dev/null || { echo "$engine is not installed in this shell." >&2; exit 1; }
build_platform=${BUILD_PLATFORM:-$(python3 "$integration_root/scripts/release.py" platform)}
case "$build_platform" in linux/amd64|linux/arm64) ;; *) echo 'BUILD_PLATFORM must be linux/amd64 or linux/arm64.' >&2; exit 1;; esac
build_flags=(--platform "$build_platform")
if [[ "$engine" == docker ]]; then
  docker buildx version >/dev/null 2>&1 || { echo 'Docker Buildx is unavailable; use CONTAINER_ENGINE=podman for Podman.' >&2; exit 1; }
  docker version --format '{{.Server.Version}}' >/dev/null 2>&1 || { echo 'Docker engine is unreachable. Start Docker Desktop/Engine.' >&2; exit 1; }
  build_flags+=(--load)
else
  # Verify rootless mappings before pulling any large images.
  if [[ $(id -u) != 0 ]]; then
    for map_name in uid_map gid_map; do
      mapping=$(podman unshare cat "/proc/self/$map_name") || exit 1
      if ! python3 -c 'import sys; sys.exit(0 if sum(int(line.split()[2]) for line in sys.stdin if line.strip()) >= 65536 else 1)' <<< "$mapping"; then
        echo "Podman $map_name lacks subordinate IDs. Allocate non-overlapping ranges in /etc/subuid and /etc/subgid, then run podman system migrate. See docs/podman-build.md." >&2
        exit 1
      fi
    done
  fi
  # Buildah defaults to 1024 open files here; Vite can misreport EMFILE as missing imports.
  build_flags+=(--format docker --layers --ulimit nofile=65536:65536)
  build_flags+=(--build-arg "BUILDPLATFORM=$build_platform" --build-arg "TARGETPLATFORM=$build_platform"
    --build-arg "TARGETOS=linux" --build-arg "TARGETARCH=${build_platform#linux/}")
fi
if [[ -n "${BUILD_PROXY_URL:-}" ]]; then
  if [[ ! "$BUILD_PROXY_URL" =~ ^http://127\.0\.0\.1:([0-9]{1,5})$ ]]; then
    echo 'BUILD_PROXY_URL must be http://127.0.0.1:PORT for a local reachable HTTP proxy.' >&2; exit 1
  fi
  proxy_port=$((10#${BASH_REMATCH[1]}))
  ((proxy_port >= 1 && proxy_port <= 65535)) || { echo 'Invalid proxy port.' >&2; exit 1; }
  if [[ "$engine" == docker ]]; then
    docker_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
    [[ "$docker_endpoint" == unix://* ]] || { echo 'Tunnel builds require a local Linux Docker socket.' >&2; exit 1; }
    [[ "$(docker info --format '{{.OSType}}')" == linux ]] || { echo 'Tunnel builds require Linux Docker Engine.' >&2; exit 1; }
    [[ "$(docker info --format '{{json .SecurityOptions}}')" != *rootless* ]] || { echo 'Docker loopback tunnel recipe requires rootful Engine.' >&2; exit 1; }
    [[ "$(docker buildx inspect default --format '{{.Driver}}')" == docker ]] || { echo 'Tunnel builds require the local default Docker builder.' >&2; exit 1; }
    build_flags+=(--builder default)
  fi
  export HTTP_PROXY="$BUILD_PROXY_URL" HTTPS_PROXY="$BUILD_PROXY_URL" ALL_PROXY="$BUILD_PROXY_URL"
  export http_proxy="$BUILD_PROXY_URL" https_proxy="$BUILD_PROXY_URL" all_proxy="$BUILD_PROXY_URL"
  export NO_PROXY="${BUILD_NO_PROXY:-localhost,127.0.0.1,::1}" no_proxy="${BUILD_NO_PROXY:-localhost,127.0.0.1,::1}"
  build_flags+=(--network host)
  for variable in HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy; do
    build_flags+=(--build-arg "$variable")
  done
fi
# Render Podman's fully qualified FROM references outside the clean product trees.
buildfile_dir=
if [[ "$engine" == podman ]]; then
  buildfile_dir=$(mktemp -d)
  trap 'rm -rf -- "$buildfile_dir"' EXIT
fi
build_image() {
  local recipe=$1
  shift
  if [[ "$engine" == podman ]]; then
    python3 "$integration_root/scripts/prepare-podman-buildfile.py" "$recipe" "$buildfile_dir/Containerfile"
    "$engine" build "${build_flags[@]}" --file "$buildfile_dir/Containerfile" "$@"
  else
    "$engine" build "${build_flags[@]}" --file "$recipe" "$@"
  fi
}
echo "Building with $engine for $build_platform"

python3 "$integration_root/scripts/release.py" verify-sources "$workspace"
headscale_image=$(python3 "$integration_root/scripts/release.py" field headscale image)
headplane_image=$(python3 "$integration_root/scripts/release.py" field headplane image)
casdoor_image=$(python3 "$integration_root/scripts/release.py" field casdoor image)
headscale_commit=$(python3 "$integration_root/scripts/release.py" field headscale source_commit)
headplane_commit=$(python3 "$integration_root/scripts/release.py" field headplane source_commit)
casdoor_commit=$(python3 "$integration_root/scripts/release.py" field casdoor source_commit)
sync_image=$(python3 "$integration_root/scripts/release.py" support-image sync)
integration_commit=$(git -C "$integration_root" rev-parse HEAD)

build_image "$integration_root/build/headscale.Dockerfile" \
  --build-arg "HEADSCALE_VERSION=${HEADSCALE_VERSION:-v0.29.3-feishu.1}" \
  --label "org.opencontainers.image.revision=$headscale_commit" \
  --tag "$headscale_image" "$workspace/headscale"
build_image "$workspace/headplane/Dockerfile" --target final \
  --build-arg "HEADPLANE_VERSION=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --build-arg "IMAGE_TAG=${HEADPLANE_VERSION:-0.7.1-feishu.1}" \
  --label "org.opencontainers.image.revision=$headplane_commit" \
  --tag "$headplane_image" "$workspace/headplane"
build_image "$workspace/casdoor/Dockerfile" --target STANDARD \
  --label "org.opencontainers.image.revision=$casdoor_commit" \
  --tag "$casdoor_image" "$workspace/casdoor"
build_image "$integration_root/build/sync.Dockerfile" \
  --label "org.opencontainers.image.revision=$integration_commit" \
  --tag "$sync_image" "$integration_root"
echo 'Local candidate images built. Export an image bundle for SSH deployment, or publish to a registry. Test before production.'
