# Fix Docker registry/build timeouts through the Windows proxy

Use this recipe on the remote Ubuntu server with **rootful Docker Engine and its local `docker:default` builder**, as shown in the reported build output. It reuses the SSH tunnel from remote `127.0.0.1:17890` to Windows `127.0.0.1:7890`. It does not expose the proxy on a public interface.

Two separate network clients need configuration:

| Client | Configuration |
| --- | --- |
| Docker daemon fetching base-image metadata/layers | Daemon proxy, followed by Docker restart |
| Build containers downloading Go/npm/apt dependencies | Proxy build arguments plus host networking |

The ordinary shell's `https_proxy` covers neither automatically. The reported GCR/Docker Hub metadata timeouts happen before compilation. The subsequent `No such image` messages simply mean the first build failed and the script never reached the other builds.

For Alibaba Cloud accelerator settings, read [mirror scope and verification](aliyun-mirror.md). A Docker Hub accelerator does not replace this GCR/build-dependency proxy path.

## 1. Keep the Windows tunnel alive and verify it

Keep the Windows proxy running on its HTTP/mixed port 7890 and retain the SSH session with `-R 127.0.0.1:17890:127.0.0.1:7890`. If it has closed, open a new PowerShell session:

```powershell
ssh -tt -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 127.0.0.1:17890:127.0.0.1:7890 "wjw@8.133.246.13"
```

Add your usual `-i` key-file argument if needed. Do not open a second tunnel on the same remote port while the first is active.

**Remote Linux shell:**

```sh
ss -ltn '( sport = :17890 )'
curl --proxy http://127.0.0.1:17890 --max-time 20 --silent --show-error \
  --output /dev/null --write-out 'GCR HTTP %{http_code}\n' https://gcr.io/v2/
curl --proxy http://127.0.0.1:17890 --max-time 20 --silent --show-error \
  --output /dev/null --write-out 'Docker Hub HTTP %{http_code}\n' https://registry-1.docker.io/v2/
```

A registry `401` is a normal unauthenticated challenge and confirms a completed HTTPS exchange; `200` is also reachable. A timeout, `000`, proxy-authentication `407`, or TLS error needs fixing before proceeding. Do not disable TLS validation.

## 2. Give the Docker daemon the temporary proxy

**Restarting Docker can interrupt existing containers, including sub2api if it runs on this same daemon. Perform the restart at an appropriate time.** The HTTP/HTTPS proxy is set in a systemd runtime drop-in and does not persist across reboot. The helper below additionally saves mirror bypass hostnames in `daemon.json`, preserving its other settings and creating a backup.

```sh
sudo mkdir -p /run/systemd/system/docker.service.d
sudo tee /run/systemd/system/docker.service.d/90-tailnet-tunnel-proxy.conf >/dev/null <<'EOF'
[Service]
Environment="HTTP_PROXY=http://127.0.0.1:17890"
Environment="HTTPS_PROXY=http://127.0.0.1:17890"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
# From the updated integration repository, add all configured mirrors to NO_PROXY.
sudo python3 scripts/sync-mirror-no-proxy.py --apply
sudo systemctl daemon-reload
sudo systemctl restart docker
docker info --format 'HTTP proxy={{.HTTPProxy}} HTTPS proxy={{.HTTPSProxy}}'
```

The helper reads both `registry-mirrors` in `/etc/docker/daemon.json` and the running local daemon's mirrors. It merges their hostnames with existing JSON/runtime/shell exclusions into `proxies.no-proxy`, preserving HTTP/HTTPS proxy URLs. Rerun it after adding mirrors; Docker does not continuously synchronize these fields. It never adds registry origins such as `gcr.io` unless they are explicitly configured as mirrors.

Expected: both effective proxy fields show `http://127.0.0.1:17890`. Settings in `daemon.json` or explicit daemon proxy flags take precedence over environment variables. If the effective values differ, inspect the existing daemon configuration and update that deliberately rather than stacking conflicting proxy settings. Keep any credentials in existing proxy configuration private.

Test the exact failing images:

```sh
docker pull golang:1.26.5
docker pull gcr.io/distroless/static-debian12:nonroot
```

Both must finish successfully before rerunning the product builds. A registry rate-limit/authentication error is different from a TCP timeout; address the actual returned error.

## 3. Update the integration scripts and build through the tunnel

From the server's integration checkout, outside `main`:

```sh
git branch --show-current
export http_proxy=http://127.0.0.1:17890
export https_proxy="$http_proxy"
git pull --ff-only
BUILD_PROXY_URL=http://127.0.0.1:17890 bash scripts/build-products.sh ..
```

The release-candidate branch includes the new build mode; verify `scripts/build-products.sh` contains `BUILD_PROXY_URL` if you are on an older/custom branch.

`BUILD_PROXY_URL` opts into the local default Docker builder and `--network host` for build `RUN` steps. It forwards upper/lowercase standard HTTP/HTTPS/ALL/NO proxy build arguments. Host networking is necessary here because a bridge-network build container's loopback is not the server host's loopback. The proxy arguments are not added as permanent image `ENV` settings.

Normal builds without `BUILD_PROXY_URL` retain their existing network behavior. The loopback mode rejects nonlocal Docker endpoints, container-based builders, and rootless Docker; their loopback/network placement needs a different recipe. Build steps have access to host networking in this mode, so use it only with the pinned, reviewed build sources.

Keep the Windows proxy and SSH connection open through all four builds. An SSH reconnect must reestablish the remote forward. Successful shell `curl` alone is not evidence that daemon pulls or builder downloads work.

## 4. Verify the outputs

After the build script exits successfully:

```sh
docker image inspect tailnet/headscale:2026.09-rc.1 --format '{{.Id}}'
docker image inspect tailnet/headplane:2026.09-rc.1 --format '{{.Id}}'
docker image inspect tailnet/casdoor:2026.09-rc.1 --format '{{.Id}}'
docker image inspect tailnet/sync:2026.09-rc.1 --format '{{.Id}}'
```

Also pull the deployment's PostgreSQL and Caddy images while the daemon proxy is available if direct registry access is unavailable. Use the exact references from `.runtime/compose.env`.

## 5. Remove the temporary daemon dependency when finished

The mirror-host exclusions saved in `daemon.json` can remain after the tunnel is removed. Once required images are available, remove only this recipe's runtime drop-in and restart Docker at an appropriate time:

```sh
sudo rm /run/systemd/system/docker.service.d/90-tailnet-tunnel-proxy.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
```

This restores the previous daemon proxy configuration, if any. Closing the SSH connection alone does not remove the daemon setting: leaving it configured would send subsequent registry operations to a dead local port. For routine production updates, use a stable server-accessible proxy rather than keeping a Windows laptop online.

## Verification scope

The repository tests verify proxy flags reach all four build invocations and unsuitable endpoints are refused. They do not simulate registry connectivity or claim the four images have been built on your server. The pull/build commands above are the live checks.

References: [Docker daemon proxies](https://docs.docker.com/engine/daemon/proxy/), [build network selection](https://docs.docker.com/reference/cli/docker/buildx/build/#network), [predefined proxy build arguments](https://docs.docker.com/build/building/variables/#proxy-arguments).
