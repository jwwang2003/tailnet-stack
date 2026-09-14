# Deployment runbook

Status: release candidate. The local test suite validates rendering and worker behavior; a real Feishu OAuth/restore pilot is a separate acceptance gate. The host needs Docker Engine with Compose v2 and Python 3.11+ (plus PyYAML for release scripts). No Docker daemon is bundled or installed by these scripts.

## Prepare the host

Use a dedicated Linux host with persistent local storage and off-host backups. Start with one active Headscale instance. Measure memory, CPU, connected devices, registration bursts, and DERP relay throughput during the pilot; employee count alone is not a capacity measurement. This is not an active-active HA design.

Provide three distinct public DNS names for Casdoor, Headscale, and Headplane, plus a distinct private MagicDNS suffix. Point the public names to the host and allow TCP 80/443 and optionally UDP 443 for Caddy. The default uses Tailscale's public DERP map; verify reachability from every user region. If deploying private DERP, add a reviewed DERP map and UDP STUN listener; this example does not deploy one.

Never place browser SSO middleware in front of the Headscale control protocol. Headplane's production configuration intentionally uses its API-only feature subset, without configuration-write integration or Docker socket access. Network/DNS configuration and policy are managed as files by operators. WebSSH/agent and in-UI configuration writes are not enabled in this baseline.

## Render configuration

From the integration repository:

```sh
cp deploy/site.example.json /tmp/feishu-site.json
# Edit /tmp/feishu-site.json with the real hostnames and organization identifiers.
python3 scripts/configure.py --site /tmp/feishu-site.json --output .runtime
```

The renderer creates a private `.runtime` directory and random database, OIDC, and cookie credentials. It preserves credentials, policy, and image pins when repeated. Review regenerated authentication configuration before using it for an existing deployment. Back up existing configuration before changing hostnames or identifiers.

Build the pinned downstream images using the build/release guide. `compose.env` uses locally built product images. Resolve image digests for Caddy, PostgreSQL, and every product before production promotion; candidate tags are not immutable supply-chain locks.

Use this shell helper only in your own session:

```sh
stack() { docker compose --env-file .runtime/compose.env -f deploy/compose.yaml "$@"; }
stack config --quiet
stack up -d db casdoor
```

## Bootstrap Casdoor privately

Casdoor is initially reachable only on the host's loopback port 8000. Use an SSH tunnel for remote setup. Keep the `public` Compose profile stopped until the built-in administrator's default password has been replaced and organization signup/access policies reviewed. If Casdoor redirects to its public origin during setup, temporarily set `origin` and `originFrontend` to the tunneled loopback URL, then restore the configured HTTPS origin and restart Casdoor before OAuth setup.

In Casdoor:

1. Replace the default built-in administrator password and enroll its recovery/MFA mechanism. Restrict who can administer Casdoor.
2. Create the organization named in `site.json`. Restrict profile `Id`, Lark binding, groups, and role properties to administrators. Employees must not edit authorization attributes.
3. Create a Lark OAuth provider for the same published Feishu custom app used by the syncer. Select Feishu endpoints, not the global Lark host. Register the redirect URI shown by Casdoor in the Feishu app console. Do not substitute the Headscale callback here.
4. Create a signing certificate with at least 2048-bit RSA keys and back it up with the Casdoor database.
5. Create the two OIDC applications with client IDs from `site.json`, organization, certificate, and secrets from `.runtime/secrets/headscale_oidc_secret` and `headplane_oidc_secret` respectively. Grant code flow, PKCE S256, scopes `openid profile email groups`; disable unneeded grants, password login, and open signup for these employee applications.
6. Enable the Lark provider on both applications. For the native-import workflow, only previously imported users may sign in; keep unrestricted signup disabled. Test both attempted login-before-import and login-after-import, ensuring no duplicate account or email auto-linking.
7. Set token format `JWT-Custom`. Select `Groups`, `Email`, `EmailVerified`, and `Properties.headplane_role`. Add Existing Field token attributes: `name` → `DisplayName`, `email_verified` → `EmailVerified`. Preserve standard registered claims and nonce. The resulting signed ID token must carry an array `groups` and a string `headplane_role` when assigned. Use the contract tests and captured redacted live evidence to verify; do not assume a field in an access token appears identically in the ID token.
8. Register callbacks exactly: `https://<headscale-host>/oidc/callback` and `https://<headplane-host>/admin/oidc/callback` on their respective applications.
9. Keep Feishu account identity tied to stable `open_id`; never automatically merge by email. Changing issuer or Casdoor user IDs later requires an explicit identity migration.

Feishu App access still depends on granted API permissions and visible organizational scope. Developer access alone does not guarantee tenant-wide directory access. Obtain any required app publication/permission approval before enabling sync. No tenant SAML/enterprise SSO configuration is required.

Use the identity contract and worker README for native syncer field ownership and group synchronization. The patched Casdoor image is required. Stock v4.3.0 has the native-import `user_id`/OAuth `open_id` binding mismatch. Do not run an independent native sync scheduler alongside the worker.

## Start authentication and register a node

Restore Casdoor's public origin, then:

```sh
stack --profile public up -d proxy
stack up -d headscale
umask 077
stack exec -T headscale headscale apikeys create --expiration 90d > .runtime/secrets/headscale_api_key
stack --profile apps up -d headplane
```

Headscale requires working Casdoor OIDC discovery at startup. On host reboot, its restart policy retries until Casdoor and public TLS routing are ready; it must not silently fall back to local registration. A broker outage prevents new enrollments; existing network behavior must be checked separately.

Verify the key file contains only the generated key (not log output); it is a server-side administrative credential. Rotate it before expiry and recreate Headplane after rotation. Its browser API-key login is disabled.

Start the sync worker only after its credentials and configuration are in place and its dry run succeeds. The first Headplane OIDC login becomes owner: restrict access during bootstrap and perform it with the intended operator before admitting other users. Keep employee defaults as `member`, not `admin` or `viewer`.

The initial network policy denies all traffic. Add narrowly scoped reviewed ACL rules to `.runtime/headscale/policy.json` before the pilot and restart Headscale to load them. OIDC groups control enrollment; they do not populate live ACL groups. File policy is the single source of truth. A future automatic policy generator must merge against a separate operator-maintained input and pass policy tests before applying.

## Pilot gate

Follow the acceptance checklist with two employees in different groups and one administrator. Verify permitted and denied resource access, user linkage, department moves, group removal, existing node revocation, and expired web sessions. Do not claim production readiness until that evidence, backup restore, and immutable image digests are recorded.
