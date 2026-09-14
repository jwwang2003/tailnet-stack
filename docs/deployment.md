# Deploy the Feishu tailnet: command-by-command guide

This guide takes a **new Ubuntu 24.04 x86-64 server** from an empty installation to a pilot with Feishu login, Casdoor user synchronization, Headscale device enrollment, and Headplane administration. Run server commands as the same non-root deployment user throughout. Steps explicitly marked **laptop** or **browser** run elsewhere.

This is the `2026.09-rc.1` deployment. Local code/configuration tests have passed; a real Feishu tenant login and a complete container/restore rehearsal have not yet been performed. The checkpoints below are intended to produce that evidence. Do not replace patched images with stock Casdoor or Headscale images.

## 1. Fill in your deployment worksheet

Replace the example hostnames everywhere with domains you control. Do not literally deploy `example.com`.

| Setting | Example | What it means |
| --- | --- | --- |
| Server SSH destination | `wjw@203.0.113.10` | Replace with the actual user and server IP |
| Casdoor hostname | `login.example.com` | Browser login and OIDC issuer |
| Headscale hostname | `vpn.example.com` | Tailscale custom coordination server |
| Headplane hostname | `admin.example.com` | Administration UI at `/admin/` |
| MagicDNS suffix | `tail.example.net` | Private tailnet names; distinct from the three hosts |
| Casdoor organization | `employees` | Keep this value for the examples below |
| Native syncer | `admin/feishu` | Casdoor object owner/name, not an employee account |
| Admission alias | `tailnet-members` | Worker creates the qualified group `employees/tailnet-members` |
| First owner | Your designated operator | The first Headplane OIDC login becomes owner |

You need permission to create/publish a Feishu custom enterprise app and grant its Contact API access. You do **not** configure Feishu enterprise SAML. If your company requires app publication or directory permissions to be approved, obtain that approval; app developer status alone does not bypass it.

Prepare a small pilot population: the first owner and one employee. Initially restrict the Feishu app's availability to the owner so another person cannot claim Headplane ownership during bootstrap.

## 2. Prepare DNS, ports, and storage

Create these DNS records, all pointing at the server's public IPv4 address:

```text
login.example.com  A  YOUR_SERVER_IPV4
vpn.example.com    A  YOUR_SERVER_IPV4
admin.example.com A  YOUR_SERVER_IPV4
```

Only add AAAA records if the server actually accepts inbound IPv6. Remove a stale AAAA record rather than leaving clients/certificate issuance pointed at an unreachable address. Use DNS-only records during the first deployment; intermediary HTTP/CDN proxying has not been validated here.

Allow in your cloud firewall/security group:

| Inbound port | Source | Purpose |
| --- | --- | --- |
| TCP 22 | Your administration IP/range | SSH and bootstrap tunnel |
| TCP 80 | Internet | Caddy certificate issuance/HTTP redirect |
| TCP 443 | Internet | Casdoor, Headscale, Headplane |
| UDP 443 | Internet, optional | Caddy HTTP/3 |

Do not publish PostgreSQL, Headscale port 8080/9090, or Headplane port 3000. Casdoor's temporary port 8000 is bound to server loopback only. Docker-published ports can bypass UFW rules, so verify the cloud firewall and actual Compose port mappings. See [Docker's firewall notes](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations).

The example uses public DERP servers; it does not deploy a local DERP/STUN server. Test relay reachability from your employees' locations. UDP 3478 is unnecessary on this host until a local DERP/STUN service is deliberately added.

Use persistent host storage for the repository's `.runtime/` directory and Docker's named volumes. Keep off-host backups. Run one active Headscale database writer. Builds include two web applications and can require substantially more memory/disk than steady-state operation; watch memory and free space rather than sizing by employee count alone.

## Optional: use your laptop's proxy while setting up the server

If your proxy application listens on **local port 7890**, run the following on your **laptop**, not on the server. Port 7890 must accept HTTP proxy requests/CONNECT (a mixed HTTP/SOCKS port works). The helper assumes an HTTP proxy, not a SOCKS-only listener.

From a local checkout of the integration repository:

```sh
bash scripts/remote-proxy-shell.sh wjw@YOUR_SERVER_IP
```

If the laptop does not have the repository, download the helper using your local proxy and inspect it before running:

```sh
curl --fail --location --proxy http://127.0.0.1:7890 \
  https://raw.githubusercontent.com/jwwang2003/tailscale-feishu-integration/release/feishu-2026.09-rc.1/scripts/remote-proxy-shell.sh \
  -o remote-proxy-shell.sh
less remote-proxy-shell.sh
bash remote-proxy-shell.sh wjw@YOUR_SERVER_IP
```

This opens a remote Bash login session with the following path:

```text
Remote Git/curl/pip
  -> remote 127.0.0.1:17890
  -> encrypted SSH reverse tunnel
  -> laptop 127.0.0.1:7890
  -> laptop proxy's outbound connection
```

The script exports `http_proxy`, `https_proxy`, `all_proxy`, and their uppercase equivalents to `http://127.0.0.1:17890`. It also sets `no_proxy`/`NO_PROXY` for loopback and internal Compose service names. The `https_proxy` value intentionally uses `http://`: the HTTP proxy carries HTTPS using CONNECT.

For a nonstandard SSH port, a different remote port, or another local proxy port:

```sh
bash scripts/remote-proxy-shell.sh --ssh-port 2222 --remote-port 17891 --local-port 7890 wjw@YOUR_SERVER_IP
```

SSH aliases and identity/jump-host settings from `~/.ssh/config` work. The script disables connection multiplexing for this session so the tunnel does not remain attached to a reused SSH master after you exit.

**In the opened remote shell, verify:**

```sh
printf 'Proxy: %s\n' "$https_proxy"
ss -ltn '( sport = :17890 )'
curl --head --fail --max-time 20 https://github.com
git ls-remote https://github.com/jwwang2003/tailscale-feishu-integration.git refs/heads/release/feishu-2026.09-rc.1
```

Expected: proxy URL `http://127.0.0.1:17890`, a loopback listener, an HTTPS response, and a Git commit/ref. Adjust the `ss` port if you selected another port. If forwarding fails, confirm the local proxy is running, the remote port is unused, and SSH server policy permits remote TCP forwarding. Keep `GatewayPorts` disabled or `clientspecified`; do not force wildcard listeners. No cloud firewall opening for port 17890 is needed.

Keep this session open while using the proxy. `exit`, laptop sleep, or a broken SSH connection ends the tunnel. Environment variables apply to this shell and child processes, not other existing SSH sessions, systemd services, or future logins. Nothing is written to your remote `.bashrc` or system proxy configuration.

Commands run through `sudo` may lose these variables. For a one-off package operation, pass them explicitly:

```sh
sudo env http_proxy="$http_proxy" https_proxy="$https_proxy" no_proxy="$no_proxy" apt-get update
```

Apply the same pattern to other root commands that need network access. Do not make the deployment depend permanently on your laptop proxy.

**Docker distinction:** this is enough for remote HTTPS Git, curl, pip, and appropriately configured host tools. It does not automatically proxy Docker image pulls or `RUN` steps in build containers. Image pulls use the daemon's own proxy settings; build containers have their own network namespace, so their `127.0.0.1` is not the server host. Do not blindly pass this loopback URL as Docker build arguments. For this stack on the remote Linux default Docker builder, follow the [Windows-tunnel Docker build recipe](docker-build-proxy.md): configure the daemon proxy for pulls, then use `BUILD_PROXY_URL=http://127.0.0.1:17890 bash scripts/build-products.sh ..` for build downloads. See [Docker daemon proxy settings](https://docs.docker.com/engine/daemon/proxy/) and [Docker build/container proxy settings](https://docs.docker.com/engine/cli/proxy/). The SSH forwarding behavior is documented in [OpenSSH's `-R` option](https://man.openbsd.org/ssh#R).

## 3. Install host tools

Skip Docker installation if a working, supported Docker Engine and Compose plugin are already installed. The following installation path assumes a fresh Ubuntu 24.04 host; it does not remove any existing container runtime.

**Server:**

```sh
sudo apt-get update
sudo apt-get install -y ca-certificates curl git python3 python3-venv python3-pip dnsutils openssl
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf '%s\n' \
  'Types: deb' \
  'URIs: https://download.docker.com/linux/ubuntu' \
  'Suites: noble' \
  'Components: stable' \
  'Architectures: amd64' \
  'Signed-By: /etc/apt/keyrings/docker.asc' \
  | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out of SSH and log back in to activate group membership. The Docker group grants host-administration authority; add only the intended deployment user. This uses [Docker's official Ubuntu repository](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).

**Checkpoint:**

```sh
docker version
docker compose version
python3 --version
df -h
```

Expected: Docker shows a Server section without permission errors, Compose is installed, and Python is 3.11 or newer. If Docker says permission denied, reconnect after the group change; do not make the Docker socket world-writable.

## 4. Get the four repositories at the matching release

Use **HTTPS Git URLs** for the server checkout so Git can use the session's HTTP proxy. Public repositories need no GitHub credentials. For private repositories, use a credential manager or enter an appropriately scoped GitHub token at Git's password prompt; do not embed tokens in clone URLs. SSH is still used to log into the server and carry the optional proxy tunnel, but GitHub checkout does not require an SSH/deploy key.

For a **new server checkout**:

```sh
mkdir -p "$HOME/tailscale-open"
cd "$HOME/tailscale-open"
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/headscale.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/headplane.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/casdoor.git
git clone --branch release/feishu-2026.09-rc.1 https://github.com/jwwang2003/tailscale-feishu-integration.git
cd tailscale-feishu-integration
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt
python scripts/release.py verify-sources ..
```

Expected: `Source checkouts match the release lock and are clean.` All three product repositories must be siblings of the integration repository. If using the existing development workspace, skip cloning and run the verification from its integration directory.

If verification reports a mismatch, compare `git -C ../casdoor rev-parse HEAD` (or the named product) with `versions.lock.yaml`. Fetch and check out the **recorded** commit on a downstream/build branch. Do not change the lock just to make an unknown checkout pass, and do not reset an existing working tree with unsaved changes.

For server repositories cloned earlier with SSH, change only their origin URLs (branches and commits stay unchanged):

```sh
cd "$HOME/tailscale-open"
for repo in headscale headplane casdoor tailscale-feishu-integration; do
  git -C "$repo" remote set-url origin "https://github.com/jwwang2003/$repo.git"
  git -C "$repo" remote get-url origin
done
cd tailscale-feishu-integration
```

All remaining **server** commands run from `~/tailscale-open/tailscale-feishu-integration` unless stated otherwise.

## 5. Generate the private runtime configuration

```sh
mkdir -p .runtime
chmod 700 .runtime
cp deploy/site.example.json .runtime/site.json
nano .runtime/site.json
```

Set the four real hostnames; retain the example identifiers unless you also change the subsequent Casdoor and worker settings:

```json
{
  "casdoor_host": "login.example.com",
  "headscale_host": "vpn.example.com",
  "headplane_host": "admin.example.com",
  "tailnet_domain": "tail.example.net",
  "organization": "employees",
  "headscale_client_id": "headscale",
  "headplane_client_id": "headplane",
  "admission_group": "tailnet-members"
}
```

Then:

```sh
python scripts/configure.py --site .runtime/site.json --output .runtime
stack() { docker compose --env-file .runtime/compose.env -f deploy/compose.yaml "$@"; }
stack --profile '*' config --quiet
```

Expected: a configuration-generated message, then no Compose validation error. The `stack` shell function is temporary; redefine it after reconnecting. Never run a bare `docker compose up` from a different directory.

Generated files:

| File/directory | Purpose |
| --- | --- |
| `.runtime/compose.env` | Image references, absolute paths, UID/GID, hostnames |
| `.runtime/casdoor/app.conf` | Casdoor database/public-origin configuration |
| `.runtime/headscale/config.yaml` | Headscale OIDC, DNS, storage configuration |
| `.runtime/headscale/policy.json` | Initially denies all network traffic |
| `.runtime/headplane/config.yaml` | UI OIDC settings and five-minute sessions |
| `.runtime/secrets/db_password` | PostgreSQL password |
| `.runtime/secrets/headscale_oidc_secret` | Headscale's future Casdoor client secret |
| `.runtime/secrets/headplane_oidc_secret` | Headplane's future Casdoor client secret |
| `.runtime/secrets/cookie_secret` | Headplane session secret |
| `.runtime/headscale/data`, `.runtime/headplane/data`, `.runtime/sync/state` | Persistent application/worker state |

The generator preserves existing secrets, policy, and image pins. It **rewrites service configuration**: back up manual configuration edits before rerendering. Do not delete `.runtime` as a troubleshooting shortcut.

## 6. Build the patched images

If registry access requires your Windows proxy, complete [the Docker tunnel setup](docker-build-proxy.md) first. Shell proxy exports alone do not cover image metadata pulls.

```sh
bash scripts/build-products.sh ..
docker image inspect feishu/headscale:2026.09-rc.1 --format '{{.Id}}'
docker image inspect feishu/headplane:2026.09-rc.1 --format '{{.Id}}'
docker image inspect feishu/casdoor:2026.09-rc.1 --format '{{.Id}}'
docker image inspect feishu/sync:2026.09-rc.1 --format '{{.Id}}'
```

Each inspect command should print an image ID. The script checks clean, matching source commits before building. Go, Node, and frontend build tools run inside the builders; a host Go installation is not required for this image path.

A build that exits 137 often needs a larger build host or more memory; inspect the build output and host memory. Registry/module download failures require resolving outbound connectivity; they are not an instruction to deploy unpatched upstream images. Record the exact source commits and resulting image IDs. Registry digests and production promotion are handled in [build/versioning](build-versioning.md).

## 7. Start Casdoor privately and replace the initial password

For the initial private bootstrap, temporarily make Casdoor's origin match the SSH tunnel:

```sh
python - <<'PY'
from pathlib import Path
p = Path('.runtime/casdoor/app.conf')
lines = p.read_text().splitlines()
p.write_text('\n'.join('origin = http://localhost:8000' if line.startswith('origin =')
    else 'originFrontend = http://localhost:8000' if line.startswith('originFrontend =')
    else line for line in lines) + '\n')
PY
stack up -d db casdoor
stack ps
stack logs --tail 60 casdoor
```

Expected: `db` is healthy, `casdoor` stays running. A missing public certificate is irrelevant at this stage. If the database connection fails, compare the generated password file and app.conf without posting them to a chat or log.

**Laptop, in a separate terminal:**

```sh
ssh -N -L 8000:127.0.0.1:8000 wjw@YOUR_SERVER_IP
```

Keep that terminal open. In your laptop browser, open [http://localhost:8000](http://localhost:8000). On a new empty database the pinned Casdoor creates organization `built-in`, username `admin`, password `123`. Sign in and immediately replace that password with a unique password; configure MFA/recovery for this operator.

Do not reuse `123` for any account. If Casdoor is using an existing database, use its existing administrator credential; these steps do not reset it.

Keep the public proxy stopped until the password is changed. Keep the built-in administrator outside the `employees` organization and Feishu sync scope.

## 8. Create the Feishu app and save its credentials

**Browser:** open the [Feishu developer console](https://open.feishu.cn/app), create a **custom enterprise app / 企业自建应用**, and name it, for example, `Company VPN`. Use the app's web-login capability; no Feishu SAML administrator configuration is involved.

Record the **App ID** (`cli_...`). Use the same app for login and directory import. In Permissions & Scopes / 权限管理, request the API capabilities needed by the actual worker calls:

| Required data | Worker/API operation | What must be returned |
| --- | --- | --- |
| Basic user identity | OAuth login and Contact users | Nonempty `open_id` |
| User department/status | `contact/v3/users/find_by_department` | `status.is_activated`, `is_frozen`, `is_resigned`, `is_exited` |
| Department tree | `contact/v3/departments/{id}/children` | `open_department_id`, name, parent |
| App Contact scope | `contact/v3/scopes` | Authorized departments/users/groups and pagination |
| Contact user groups, if used | `contact/v3/group/simplelist`, group member `simplelist` | Selected group IDs and complete members |
| Email, if desired | Contact/OAuth profile | `email` or `enterprise_email`, under the applicable field permission |

The native import's documented starting permissions are `contact:user.base:readonly` and `contact:department.base:readonly`. Additional fields and group/scope endpoints may require further grants. Use the permission list on each endpoint in the Feishu API Explorer to select the permissions available to **your tenant**, then publish a new app version and approve its data scope. Do not assume those two base permissions expose employee status, groups, or the whole company.

For the simplest first pilot, use **department-only admission**; Contact-group permissions can be added later. Restrict app availability to the first owner during bootstrap, while granting the directory scope needed to inspect the pilot departments. App availability and Contact data scope are separate settings.

**Server:** save secrets using an interactive hidden prompt, not shell arguments:

```sh
python - <<'PY'
from getpass import getpass
from pathlib import Path
p = Path('.runtime/secrets/feishu_app_secret')
value = getpass('Feishu App Secret: ').strip()
if not value:
    raise SystemExit('No secret supplied')
p.write_text(value)
p.chmod(0o600)
PY
```

App ID is not the secret. Do not put the App Secret in `site.json`, source files, or Git.

## 9. Create the Casdoor organization, certificate, and Lark provider

**Browser, still through the private tunnel:**

1. Open **Organizations**, add an organization with **Name** `employees` and a recognizable display name. Keep user IDs immutable. Set account modification rules for provider bindings, groups, and custom properties to administrator-only. Employees must not edit `headplane_role`, `feishu_sync_*`, or `lark`.
2. Open **Certificates**, add a JWT-signing certificate, choose RSA of at least 2048 bits, and give it a name such as `cert-tailnet`. Record the name. Do not use weak-key compatibility in Headplane.
3. Open **Providers**, add an **OAuth** provider with **Type = Lark**, **Name = feishu**, **Client ID = your cli_... App ID**, and **Client secret = the Feishu App Secret**. Set **Use global endpoint = off/false** so it uses `open.feishu.cn`.
4. In the Feishu app console, configure the web-login redirect URI as `https://login.example.com/callback` (replace the hostname). This is Casdoor's Lark callback. The Headscale and Headplane callbacks belong in Casdoor applications, not in the Feishu app.
5. Save and publish any changed Feishu app settings. Keep availability limited to the bootstrap owner until ownership setup is complete.

Do not create a scheduled native syncer yet; step 12 creates it with the exact field ownership the worker requires.

## 10. Create the two Casdoor OIDC applications

**Browser → Applications → Add**, create each employee-facing application using the following values:

| Field | Headscale application | Headplane application |
| --- | --- | --- |
| Name | `app-headscale` | `app-headplane` |
| Organization | `employees` | `employees` |
| Client ID | `headscale` | `headplane` |
| Client secret | File `headscale_oidc_secret` | File `headplane_oidc_secret` |
| Certificate / Token cert | `cert-tailnet` | `cert-tailnet` |
| Redirect URI | `https://vpn.example.com/oidc/callback` | `https://admin.example.com/admin/oidc/callback` |
| Token format | `JWT-Custom` | `JWT-Custom` |
| Providers | `feishu` | `feishu` |
| Password sign-in | Disabled | Disabled |
| Open sign-up | Disabled | Disabled |
| Provider Can sign in | Enabled | Enabled |
| Provider Can sign up / unlink | Disabled | Disabled |

Use the secret files generated in step 5; do not generate different client secrets in Casdoor without updating those files. Open the files privately in an editor/password manager and paste into the appropriate fields. Never send these secrets to an employee.

In the OIDC/OAuth tab, set Grant types to **Authorization Code** (add Refresh Token only if you later need it), remove the default localhost redirect, and keep **Is shared** off. Casdoor has no application-level Enable PKCE switch: it verifies the S256 challenge/verifier sent by Headscale and Headplane automatically. Request scopes `openid profile email groups`; both application clients already request these in the generated YAML. Do not enable password/client-credentials grants on the employee OIDC apps just to troubleshoot a browser login.

For **both** applications, configure the JWT field/attribute mapping:

| Mapping | Value |
| --- | --- |
| Token fields | `Groups`, `Email`, `EmailVerified`, `Properties.headplane_role` |
| Token attribute `name` | Category `Existing Field`, Value `DisplayName`, Type `String` |
| Token attribute `email_verified` | Category `Existing Field`, Value `EmailVerified` |

`Properties.headplane_role` becomes the flat `headplane_role` claim. `Groups` must become an array, e.g. `["employees/tailnet-members"]`, not a comma-separated string. The format preserves standard issuer, subject, audience, expiry, and nonce fields. The current UI may not offer `EmailVerified` in its Existing Field dropdown; the API procedure in step 11 sets the supported backend mapping explicitly. Verify a real ID token and UserInfo response during the pilot. Do not change `email_verified` to a constant true to bypass missing profile permissions.

Default employees get role `member`, which has **no Headplane UI access**. The first owner is bootstrapped separately; later operator roles come from the explicit worker mapping or a reviewed manual assignment.

## 11. Create the worker's Casdoor API application

Create a **separate** application for server-to-server worker access, with client ID such as `feishu-sync`. Its secret is unrelated to the Feishu App Secret or either OIDC client secret. Use Name `app-feishu-sync`, Owner `admin`, Organization `employees`, Client ID `feishu-sync`, Is shared **off**, and only **Client Credentials** in Grant types. Disable interactive signup/password login and leave its provider list empty. Save and record its generated client secret.

**Actual privilege boundary in this release:** Casdoor recognizes these app credentials as an application administrator with broad management API access. The Organization dropdown does not restrict them to that organization, and no additional Permission row is needed for these API calls. Keep them only on the server. The worker restricts its own writes by organization and ownership checks; that is not a server-enforced credential scope. The same Casdoor rule makes the downstream OIDC client secrets sensitive management credentials too. Do not distribute them to employees.

Save the service application's Client secret:

```sh
python - <<'PY'
from getpass import getpass
from pathlib import Path
p = Path('.runtime/secrets/casdoor_sync_client_secret')
value = getpass('Casdoor worker application Client secret: ').strip()
if not value:
    raise SystemExit('No secret supplied')
p.write_text(value)
p.chmod(0o600)
PY
```

Create this local API helper **on the server**, from the integration directory. It uses Casdoor's loopback listener and reads the secret from its file. It prints only status unless you explicitly save a response to a private file:

```sh
cat > .runtime/casdoor-api.py <<'PY'
import argparse, base64, json, os, re
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Unexpected redirect from loopback API')
p = argparse.ArgumentParser()
p.add_argument('action')
p.add_argument('--query', action='append', default=[])
p.add_argument('--body', type=Path)
p.add_argument('--output', type=Path)
a = p.parse_args()
assert re.fullmatch(r'[a-z-]+', a.action)
query = dict(item.split('=', 1) for item in a.query)
credential = 'feishu-sync:' + Path('.runtime/secrets/casdoor_sync_client_secret').read_text().strip()
headers = {'Authorization': 'Basic ' + base64.b64encode(credential.encode()).decode(),
           'Content-Type': 'application/json'}
body = a.body.read_bytes() if a.body else None
url = 'http://127.0.0.1:8000/api/' + a.action + ('?' + urlencode(query) if query else '')
request = Request(url, data=body, headers=headers, method='POST' if body else 'GET')
with build_opener(NoRedirect()).open(request, timeout=60) as response:
    result = json.load(response)
if result.get('status') != 'ok':
    raise SystemExit('Casdoor rejected the request; inspect the operation and server logs.')
if a.output:
    fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result.get('data'), output, indent=2)
    a.output.chmod(0o600)
print('OK:', a.action)
PY
chmod 600 .runtime/casdoor-api.py
python .runtime/casdoor-api.py get-users --query owner=employees
```

Expected: `OK: get-users`. This confirms API authentication; it does not verify Feishu yet. If you chose a different service Client ID, change the `credential` prefix and the worker configuration consistently.

Use the helper to apply the precise token mapping to **existing** OIDC application objects, preserving their other settings:

```sh
python .runtime/casdoor-api.py get-application --query id=admin/app-headscale --output .runtime/headscale-application.json
python .runtime/casdoor-api.py get-application --query id=admin/app-headplane --output .runtime/headplane-application.json
python - <<'PY'
import json
from pathlib import Path
attributes = [
    {'name':'name', 'category':'Existing Field', 'value':'DisplayName', 'type':'String'},
    {'name':'preferred_username', 'category':'Existing Field', 'value':'Name', 'type':'String'},
    {'name':'picture', 'category':'Existing Field', 'value':'Avatar', 'type':'String'},
    {'name':'email_verified', 'category':'Existing Field', 'value':'EmailVerified', 'type':'Boolean'}
]
for filename in ('headscale-application.json', 'headplane-application.json'):
    path = Path('.runtime') / filename
    app = json.loads(path.read_text())
    assert app['organization'] == 'employees'
    app['tokenFormat'] = 'JWT-Custom'
    app['tokenFields'] = ['Groups', 'Email', 'EmailVerified', 'Properties.headplane_role']
    app['tokenAttributes'] = attributes
    path.write_text(json.dumps(app, indent=2))
PY
python .runtime/casdoor-api.py update-application --query id=admin/app-headscale --body .runtime/headscale-application.json
python .runtime/casdoor-api.py update-application --query id=admin/app-headplane --body .runtime/headplane-application.json
```

These files contain client secrets: keep them in `.runtime`. The `email_verified` value remains the user's actual boolean; native Feishu import does not automatically verify email. This deployment uses stable subjects and admission groups, so do not fabricate email verification. Headscale may omit an unverified email from its profile; that does not prevent subject-based linking. If you add email/domain filters later, define a genuine email-verification procedure first.

## 12. Configure the native importer and the worker

Start from the worker example:

```sh
cp sync/config.example.json .runtime/sync/sync.json
chmod 600 .runtime/sync/sync.json
nano .runtime/sync/sync.json
```

For a first **department-only** pilot, use this shape with your values:

```json
{
  "feishu": {
    "base_url": "https://open.feishu.cn",
    "app_id": "cli_YOUR_APP_ID",
    "app_secret_file": "/run/secrets/feishu_app_secret",
    "tenant_key": "your-company-directory",
    "root_department_ids": ["0"],
    "group_ids": []
  },
  "casdoor": {
    "base_url": "http://casdoor:8000",
    "organization": "employees",
    "client_id": "feishu-sync",
    "client_secret_file": "/run/secrets/casdoor_sync_client_secret",
    "native_syncer_id": "admin/feishu"
  },
  "allow_group_id": "",
  "allowed_department_ids": ["od_YOUR_PILOT_DEPARTMENT"],
  "admission_group": "tailnet-members",
  "headplane_role_groups": {},
  "state_file": "/state/sync-state.json",
  "interval_seconds": 300,
  "missing_confirmations": 2,
  "allow_reenable": false,
  "http_timeout_seconds": 30,
  "http_attempts": 3
}
```

`tenant_key` here binds local state to an operator-selected tenant label; the worker does not attest this string through the API. Feishu app credentials and granted scope determine the real tenant boundary. Keep the label stable.

Get the pilot **open_department_id** from the Feishu API Explorer by calling “Get department / 获取部门信息” or “Get sub-departments / 获取子部门列表” with `department_id_type=open_department_id`. Root `0` is a traversal root, not an allowed admission department. `od_...` is an identifier; a department name such as `Engineering` is not interchangeable. If your app cannot read the whole tree, use the explicitly granted root department IDs instead of `0`.

For **Contact-group admission** instead, list groups using `GET /open-apis/contact/v3/group/simplelist` in API Explorer, take the group's `id`, add it to `feishu.group_ids`, and set `allow_group_id` to that same ID. These are Contact user groups, not Feishu chat IDs. Set `allowed_department_ids: []` if you want the Contact group to be the only admission rule. When both are configured, admission is an **OR**.

To map a separate admin group, include its ID in `feishu.group_ids` and set, for example, `"headplane_role_groups": {"g_NETWORK_OPERATORS": "network_admin"}`. Group IDs vary; use the actual returned ID. The role mapping does not itself grant tailnet admission.

Create native syncer `admin/feishu` with:

| Field | Value |
| --- | --- |
| Owner / Name | `admin` / `feishu` |
| Organization | `employees` |
| Type | `Lark` |
| Host | `https://open.feishu.cn` |
| User | Feishu `cli_...` App ID |
| Password | Feishu App Secret |
| Is enabled | `false` — worker schedules import |
| Is read only | `true` — never write back to Feishu |
| Sync interval | `300` (scheduler remains disabled) |

Use these exact columns:

```json
[
  {"name":"Lark","casdoorName":"Lark","isKey":true,"isHashed":false},
  {"name":"DisplayName","casdoorName":"DisplayName","isHashed":true},
  {"name":"Email","casdoorName":"Email","isHashed":true},
  {"name":"Avatar","casdoorName":"Avatar","isHashed":true},
  {"name":"Title","casdoorName":"Title","isHashed":true}
]
```

**Important:** this Casdoor release hides Host for Lark in the UI. Use the API/import procedure below to set and verify it; leaving Host blank selects global Lark and makes the worker reject the syncer. Do not choose Email or DisplayName as the key. Do not add `Id`, `Name`, `Groups`, `Properties`, or `IsForbidden` columns.

Create the native syncer using the service API helper. This reads the app secret privately and creates a JSON object with the otherwise hidden Host field:

```sh
python - <<'PY'
import json
from pathlib import Path
worker = json.loads(Path('.runtime/sync/sync.json').read_text())
columns = [{'name':'Lark','casdoorName':'Lark','type':'string','isKey':True,'isHashed':False}]
columns += [{'name':name,'casdoorName':name,'type':'string','isHashed':True}
            for name in ('DisplayName','Email','Avatar','Title')]
syncer = {'owner':'admin','name':'feishu','organization':'employees','type':'Lark',
          'host':'https://open.feishu.cn','user':worker['feishu']['app_id'],
          'password':Path('.runtime/secrets/feishu_app_secret').read_text().strip(),
          'isEnabled':False,'isReadOnly':True,'syncInterval':300,'tableColumns':columns}
p = Path('.runtime/native-syncer.json')
p.write_text(json.dumps(syncer, indent=2))
p.chmod(0o600)
PY
python .runtime/casdoor-api.py add-syncer --body .runtime/native-syncer.json
python .runtime/casdoor-api.py get-syncer --query id=admin/feishu --query organization=employees --output .runtime/native-syncer-check.json
```

For a fresh setup, `add-syncer` should report OK. If `admin/feishu` already exists, inspect that object and use `update-syncer --query id=admin/feishu --body .runtime/native-syncer.json` only when it is the importer you intend to replace. Verify `host`, `user`, `isEnabled`, `isReadOnly`, and the columns in the saved check file privately. Do not enable its native scheduler: the worker invokes it serially.

Run the first dry run:

```sh
stack --profile sync run --rm worker --config /config/sync.json
```

Expected: one JSON report with `"status":"ok"`, `"mode":"dry-run"`. On an empty organization, `unlinked_source_users` is expected: dry run does not create users. Confirm department/user counts match the granted population. If there is any error, use the troubleshooting table before applying.

Then perform the first import:

```sh
stack --profile sync run --rm worker --config /config/sync.json --apply
```

Expected: `"status":"ok"`, `"mode":"apply"`; `unlinked_source_users` should be zero for the selected scope. In Casdoor → Users → employees, inspect the owner account: `lark` must equal Feishu `open_id`, ID must be nonempty, and groups must include `employees/tailnet-members`. Group names are stable ID-based names, such as `feishu-department-od_...`.

Repeat `--apply`; with no changes the worker should converge to `operations: 0`. If not, inspect the specific import/membership change rather than starting the schedule immediately.

## 13. Switch Casdoor to public HTTPS

Restore the public origins from the saved site worksheet:

```sh
python - <<'PY'
import json
from pathlib import Path
host = json.loads(Path('.runtime/site.json').read_text())['casdoor_host']
p = Path('.runtime/casdoor/app.conf')
p.write_text('\n'.join('origin = https://' + host if line.startswith('origin =')
    else 'originFrontend = https://' + host if line.startswith('originFrontend =')
    else line for line in p.read_text().splitlines()) + '\n')
PY
# useGroupPathInToken is a Casdoor SERVER setting, not an application switch.
printf '\nuseGroupPathInToken = false\n' >> .runtime/casdoor/app.conf
stack restart casdoor
stack --profile public up -d proxy
stack logs --tail 80 proxy
```

The proxy may log failures for Headscale/Headplane, which are not running yet. The Casdoor hostname must work before proceeding.

**Checkpoint, using your real hostname:**

```sh
curl --fail --silent --show-error https://login.example.com/.well-known/openid-configuration \
  | python -m json.tool
```

Expected: JSON containing issuer `https://login.example.com`, authorization/token/UserInfo/JWKS endpoints. Do not use `curl -k` to hide a certificate failure. Fix DNS, port reachability, or Caddy issuance first. Both host and containers must be able to reach the public issuer.

Open Casdoor at its public hostname and confirm the changed administrator password works. The Feishu redirect URI must now match this public hostname.

## 14. Start Headscale and generate its administrative API key

```sh
stack up -d headscale
stack ps headscale
stack logs --tail 80 headscale
stack exec -T headscale headscale health
```

Expected: service healthy and health command exit status 0. Headscale requires successful OIDC discovery; if Casdoor or TLS is unavailable it restarts rather than silently using local registration. Diagnose issuer reachability before proceeding.

Generate the key directly into its private file:

```sh
umask 077
stack exec -T headscale headscale apikeys create --expiration 90d > .runtime/secrets/headscale_api_key
chmod 600 .runtime/secrets/headscale_api_key
python - <<'PY'
from pathlib import Path
s = Path('.runtime/secrets/headscale_api_key').read_text().strip()
assert s and not any(c.isspace() for c in s), 'Expected one API key, not log output'
print('API key file is nonempty and contains one value; key not printed.')
PY
```

Do not generate this key again on every restart. It is an administrative credential used by Headplane's backend. Note its 90-day expiry and follow the rotation instructions in [operations](operations.md).

## 15. Enroll the first device and bootstrap Headplane ownership

**Owner's laptop**, after installing an IT-approved Tailscale client:

```sh
tailscale login --login-server https://vpn.example.com
```

Follow the browser redirect → Casdoor → Feishu, choose the correct company account, and confirm the device registration. On Linux, you may need `sudo tailscale login ...`. Other client paths are in the [employee handbook](employee-handbook.md).

**Server:**

```sh
stack exec -T headscale headscale users list
stack exec -T headscale headscale nodes list
```

Expected: the owner exists as an OIDC user and has a registered device. Enrolling before UI login makes Headplane's user matching easier to verify. Missing email alone should not make you replace the stable subject with email; check the actual claim contract.

Start Headplane:

```sh
stack --profile apps up -d headplane
stack ps headplane
stack logs --tail 80 headplane
```

Keep Feishu app availability restricted to the owner. Open `https://admin.example.com/admin/` and sign in through Feishu with that same account. Confirm the account has **Owner** and links to the existing Headscale user/device.

Only after ownership is confirmed, expand Feishu app availability to the pilot employee(s). Ordinary employees receive `member` and therefore no Headplane UI access. Do not make everyone an administrator just to make a page visible.

Headplane runs with API access only. DNS/configuration editing from its UI, WebSSH, and the agent are not enabled here. No Docker socket is mounted.

## 16. Permit one pilot resource

The generated policy deliberately denies all network traffic. A Connected client is not proof the employee can reach an approved resource.

For a simple two-device pilot, register a server/resource node under an authorized account, record its actual Tailscale IPv4 address from `headscale nodes list`, and permit one employee to reach a single port. The following is a **shape to edit**, not a permission to give a subnet to all employees:

```json
{
  "groups": {
    "group:pilot": ["CASDOOR_ISSUER/EXACT_USER_SUBJECT@"]
  },
  "hosts": {
    "pilot-resource": "100.64.0.2"
  },
  "acls": [
    {"action":"accept", "src":["group:pilot"], "dst":["pilot-resource:443"]}
  ]
}
```

Use the exact Headscale OIDC provider identifier for that user, appending `@` when the identifier contains no `@`, as described in the pinned [Headscale OIDC policy reference](https://github.com/juanfont/headscale/blob/v0.29.3/docs/ref/oidc.md). Do not use the display name as an identity key. Replace `100.64.0.2` with the real resource IP and ensure a service actually listens on TCP 443.

Back up `.runtime/headscale/policy.json`, edit it, validate the JSON with `python -m json.tool .runtime/headscale/policy.json >/dev/null`, and restart Headscale to load this file. Check logs for policy errors. JSON parsing alone does not validate policy semantics.

Test the allowed service and a second port/resource that should remain denied. OIDC `allowed_groups` controls who can enroll; it does not populate `group:pilot` or continuously rewrite ACLs.

## 17. Start ongoing synchronization and make a backup

After both imports converge and the owner/device checks pass:

```sh
stack --profile sync up -d worker
stack --profile '*' ps
stack logs --tail 50 worker
```

Expected: a successful JSON report approximately every 300 seconds. Alert if no full successful run occurs for two intervals. A permission-scope change intentionally stops reconciliation and requires operator review; do not erase state to make the error disappear.

Create a protected backup outside `.runtime`:

```sh
mkdir -p "$HOME/feishu-backups"
chmod 700 "$HOME/feishu-backups"
bash scripts/backup.sh .runtime "$HOME/feishu-backups/pilot-before-rollout.tar.gz"
```

Use a new filename each time. The script briefly stops application/proxy writers, dumps PostgreSQL, copies runtime/key state and Caddy volumes, then restarts the original running services. The archive contains secrets; mode 600 is not encryption. Store an encrypted off-host copy using your backup system.

Exercise a fresh-project restore with [the operations runbook](operations.md) before broad rollout. Do not run `docker compose down -v` on a deployment you intend to retain.

## 18. Troubleshooting by checkpoint

| Symptom | Inspect | Resolution |
| --- | --- | --- |
| Docker permission denied | `id`, Docker group membership | Reconnect SSH after group change |
| Source-lock mismatch | Named repo's `HEAD`, `versions.lock.yaml` | Use the recorded downstream commit |
| Casdoor restart loop | `stack logs --tail 80 casdoor db` | Check database credentials/volume permissions; keep generated UID/GID consistent |
| Private browser redirects to HTTPS too early | `origin` and `originFrontend` | Use step 7's temporary loopback origin, then restart Casdoor |
| Native syncer wrong endpoint | Stored syncer `host` | Explicitly set `https://open.feishu.cn` through API/import |
| Worker rejects columns | Native `tableColumns` | Exactly one Lark key; profile fields hashed; no worker-owned fields |
| Worker missing status/scope/group data | Feishu API Explorer response and grants | Grant/publish required endpoint and field permissions; check data scope |
| `unlinked_source_users` stays nonzero after apply | Casdoor `lark`, native logs, app ID | Use patched Casdoor and the same Feishu app; audit old wrong bindings, never auto-merge email |
| OAuth redirect mismatch | Browser redirect destination, both consoles | Feishu → Casdoor `/callback`; each Casdoor client → its own downstream callback |
| Headscale restart loop | `stack logs --tail 80 headscale` and issuer discovery | Fix Casdoor/DNS/TLS; do not enable fallback authentication |
| Headplane invalid API key | Key file/expiry and server URL | Generate/rotate a valid server key and recreate Headplane |
| Employee cannot open Headplane | Expected role | `member` has no UI; verify only authorized operator mappings |
| Connected but resource unreachable | Policy, resource port, route, DNS | Start with one explicit IP/port test and inspect Headscale logs |
| Role/account change did not cut an existing VPN connection | Existing nodes/preauth keys | Revoke those separately per offboarding runbook; broker disablement is not device revocation |

## 19. What qualifies this for production

Complete [the acceptance checklist](acceptance-checklist.md): real Feishu login, both OIDC clients, identity preservation, role boundaries, nested/multiple departments, a membership change, a disabled employee, existing device/session revocation, a restore drill, and an employee walkthrough.

Record actual container image digests and the test evidence in a copy of `releases/manifest.example.yaml`. The checker refuses incomplete production promotion. Keep the current RC status until those checks pass; creating or pushing a release branch does not prove a production deployment.
