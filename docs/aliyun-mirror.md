# Alibaba Cloud accelerator: scope and verification

The following is valid Docker configuration syntax, using the accelerator URL supplied for this deployment:

```json
{
  "registry-mirrors": ["https://mra3ydig.mirror.aliyuncs.com"]
}
```

It does not establish that the endpoint is reachable, that it contains the required tags, or that the whole build can complete. The supplied endpoint is `https://mra3ydig.mirror.aliyuncs.com`. A direct `/v2/` probe from the development environment returned HTTP 403 on 2026-09-14. That is not a probe from your ECS server and does not establish whether ECS pulls work. No successful ECS pull is claimed.

## Current limitations relevant to Fysics

Alibaba's current documentation says the accelerator has stopped synchronizing the latest images; it limits the service to personal development and states non-ECS access can return HTTP 403. Its service-change notice also says specific versions cannot be guaranteed. Treat this as an optional developer convenience, not the company's production image distribution strategy.

| Required download | Covered by this Docker daemon setting? |
| --- | --- |
| Docker Hub `golang:1.26.5`, `golang:1.26.6`, Node, Python, Alpine | Potentially; required tags must actually be available |
| `gcr.io/distroless/static-debian12:nonroot` | No |
| `gcr.io/distroless/nodejs24-debian13:latest` | No |
| Go modules, npm/Yarn packages, apt/apk repositories during builds | No |

The stack's local default Docker builder uses daemon registry settings. A separate container/Kubernetes BuildKit builder requires its own registry configuration; do not assume this file configures every builder.

## Configure without discarding existing settings

On the remote ECS server, back up `/etc/docker/daemon.json` if it exists, then edit it. Add or update only `registry-mirrors`; preserve existing `proxies`, logging, data-root, runtime, and other configuration. Do not overwrite an existing file with the standalone JSON example above.

For example, back up to a new timestamped filename before editing:

```sh
sudo mkdir -p /etc/docker
if sudo test -f /etc/docker/daemon.json; then
  sudo cp -a /etc/docker/daemon.json "/etc/docker/daemon.json.before-mirror.$(date +%Y%m%d%H%M%S)"
fi
sudoedit /etc/docker/daemon.json
sudo dockerd --validate --config-file=/etc/docker/daemon.json
```

Do not apply a file rejected by validation. Docker also rejects a setting duplicated between daemon startup flags and the JSON file; standalone file validation does not check all service flags.

Modern Docker supports reloading `registry-mirrors`. For the standard systemd installation:

```sh
sudo systemctl reload docker
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

Expected: the exact accelerator URL is listed. If reload is unsupported by your service unit, schedule a Docker restart and check again. A restart can interrupt existing services such as sub2api; changing this mirror entry does not justify an unplanned service interruption.

## Interaction with the Windows SSH proxy

If the Docker daemon uses the Windows tunnel, mirror requests must bypass it so they originate directly from ECS. This is now part of the default documented proxy setup: use the helper to add **all configured registry mirror hostnames** automatically.

From the remote server's integration checkout:

```sh
export http_proxy=http://127.0.0.1:17890
export https_proxy="$http_proxy"
git pull --ff-only
# Preview the exact hostnames and merged exclusion list:
sudo python3 scripts/sync-mirror-no-proxy.py
# Validate with dockerd, back up daemon.json, and save:
sudo python3 scripts/sync-mirror-no-proxy.py --apply
```

The helper reads `/etc/docker/daemon.json` and the running local Docker daemon. For your mirror it adds `mra3ydig.mirror.aliyuncs.com`, without `https://` or a trailing slash. It preserves other mirror entries, existing exclusions, HTTP/HTTPS proxy URLs, and unrelated daemon settings. It writes the effective exclusion list to `proxies.no-proxy` in `daemon.json`; this takes precedence over a systemd `NO_PROXY` environment value. No wildcard for all Alibaba domains is added.

Example resulting configuration shape (other existing keys remain):

```json
{
  "registry-mirrors": ["https://mra3ydig.mirror.aliyuncs.com"],
  "proxies": {
    "no-proxy": "localhost,127.0.0.1,::1,mra3ydig.mirror.aliyuncs.com"
  }
}
```

This command does not start or restart Docker. Apply it at a suitable time because restarting Docker can interrupt existing containers, including sub2api:

```sh
sudo systemctl restart docker
docker info --format '{{.NoProxy}}'
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

Both the mirror configuration and the exclusion hostname should appear. The helper merges mirrors **when run**, not continuously: rerun after adding/changing mirrors. It intentionally keeps old exclusions, including removed mirrors, rather than deleting what might be an operator rule; clean obsolete entries up explicitly if needed. If Docker is stopped, `--configured-only` reads only the file and shell exclusions, without running `docker info`.

The mirror can return 403 if its requests exit through the laptop/proxy rather than an eligible ECS source. Do not expose the SSH proxy to public interfaces to work around this.

## Verify the exact build requirements

First check this endpoint directly from ECS:

```sh
curl --noproxy '*' --max-time 20 --silent --show-error \
  --output /dev/null --write-out 'Mirror HTTP %{http_code}\n' \
  https://mra3ydig.mirror.aliyuncs.com/v2/
```

A 200 or an authentication challenge only proves an HTTP/TLS exchange, not image availability. A 403 requires checking accelerator eligibility/source routing. A timeout is a connectivity failure.

Then test the required images separately:

```sh
docker pull golang:1.26.5
docker pull golang:1.26.6
docker pull gcr.io/distroless/static-debian12:nonroot
docker pull gcr.io/distroless/nodejs24-debian13:latest
```

The first two exercise Docker Hub resolution. The latter two still require working GCR access, such as the configured daemon proxy. A successful Docker Hub pull does not prove the mirror served it: Docker may fall back to Docker Hub, possibly through the proxy. Establishing mirror use specifically requires request/access-log evidence, not just the `docker info` listing.

If a required tag is absent or stale, do not silently downgrade it to an available image. Keep the pinned source/toolchain requirements and use the proxy recipe or copy the verified images into a company-controlled registry such as an appropriately configured ACR instance. Registry mirroring alone also does not fix dependency downloads inside builds; use [the build proxy mode](docker-build-proxy.md).

## Primary references

- [Alibaba accelerator configuration and current restrictions](https://help.aliyun.com/zh/acr/user-guide/accelerate-the-pulls-of-docker-official-images)
- [Alibaba accelerator service-change notice](https://help.aliyun.com/zh/acr/product-overview/product-change-acr-mirror-accelerator-function-adjustment-announcement)
- [Docker Hub mirror scope](https://docs.docker.com/docker-hub/image-library/mirror/)
- [Reloadable Docker daemon settings](https://docs.docker.com/reference/cli/dockerd/#configuration-reload-behavior)
- [Separate BuildKit registry configuration](https://docs.docker.com/build/buildkit/configure/)
