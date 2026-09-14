# Deploy Integrated Tailnet

The core is Headscale, Headplane, Casdoor, PostgreSQL, and Caddy. Casdoor supplies
OIDC to both network services and can use locally managed accounts or a configured
identity provider. Choose and test that identity source before admitting employees.
The provided worker supports Feishu only and is optional: no worker configuration,
Feishu secret, native Lark syncer, or Feishu API application is needed for the core.

This is a candidate deployment. Local checks cover configuration and implementation
contracts; a live provider, container deployment, and restore rehearsal still need
site-specific evidence. Keep the source tuple from `versions.lock.yaml` together.

## Build and render

Use a Linux deployment host with Docker Compose, persistent storage, and three
public DNS names for Casdoor, Headscale, and Headplane. The tailnet DNS suffix must
be distinct. The supplied proxy publishes ports 80/443; database and management
ports remain private. Use the [local build/delivery guide](local-build-deploy.md)
or [build helpers](build-versioning.md), then render private configuration:

```sh
umask 077
mkdir -p .runtime
cp deploy/site.example.json .runtime/site.json
# Edit .runtime/site.json: real hostnames, organization, clients, admission group.
python3 scripts/configure.py --site .runtime/site.json --output .runtime
stack() { docker compose --env-file .runtime/compose.env -f deploy/compose.yaml "$@"; }
stack config --quiet
```

For offline delivery, add `-f deploy/compose.offline.yaml` to the function after
importing the matching image bundle. Always use the runtime's `compose.env`; its
project name owns persistent volumes. Read [migration notes](branding-migration.md)
when upgrading an existing installation.

The renderer creates OIDC/client/cookie/database secrets and service settings.
It does not configure Casdoor applications or grant users admission. The generated
network policy initially denies all traffic. Keep manual application configuration
changes backed up before rerendering.

## Bootstrap Casdoor and choose identity

Start Casdoor privately and replace its initial administrator password using
[private bootstrap](deployment.md#7-start-casdoor-privately-and-replace-the-initial-password).
That procedure temporarily changes origins to `http://localhost:8000`, starts only
`db casdoor`, and uses an SSH tunnel. Keep the public proxy stopped until complete.

Create organization `employees` (or the name in your site file), an RSA signing
certificate of at least 2048 bits, and the two confidential OIDC applications.
Use the [application settings](deployment.md#10-create-the-two-casdoor-oidc-applications)
with these provider-neutral choices:

- For an external provider, configure its Casdoor provider and approved callback;
  select it on both applications. Restrict sign-in to provisioned users and disable
  unrestricted signup and provider unlinking. Actual provider support and claim
  behavior must be verified against the pinned Casdoor build.
- For Casdoor-managed accounts, provision the pilot accounts administratively and
  enable the intended local sign-in method on the two applications. Their provider
  list need not contain Feishu. Establish password/MFA/recovery and offboarding
  procedures for those accounts before opening enrollment.
- Keep the emergency administrator separate from employee identity and directory
  ownership. Restrict application availability to the designated first owner until
  Headplane ownership is established.

Use the generated client IDs/secrets and exact callbacks:

| Client | Callback | Requested scopes |
| --- | --- | --- |
| Headscale | `https://vpn.example.com/oidc/callback` | `openid profile email groups` |
| Headplane | `https://admin.example.com/admin/oidc/callback` | `openid profile email groups` |

Replace hostnames with the site values. Use Authorization Code and S256 PKCE;
remove unused redirect URIs and keep application sharing off. Configure JWT-Custom
with token fields `Groups`, `Email`, `EmailVerified`, `Properties.headplane_role`,
plus `name` from `DisplayName` and boolean `email_verified` from `EmailVerified`.
The [API mapping example](deployment.md#11-create-the-workers-casdoor-api-application)
shows exact backend attribute fields if the UI omits an option; any API credential
used for core setup is an administrator provisioning credential, not a required
Feishu worker. Revoke setup-only credentials when finished.

Set `useGroupPathInToken = false` in `.runtime/casdoor/app.conf`. For each pilot
user, create/assign the Casdoor Virtual group named `tailnet-members` under the
organization, so `groups` contains `employees/tailnet-members` (or the site's
qualified admission group). Without a directory adapter, reviewed manual Casdoor
membership assignments own admission. The worker is not needed to create this group.
Keep user IDs immutable, and restrict group/property/provider-binding edits to
administrators. Do not link separate identities only because emails match.

Both clients must receive the same immutable subject for one employee. Validate
issuer, audience, expiry, signatures, nonce, the group array, and actual email
verification in ID tokens and UserInfo. Never synthesize a true email-verification
claim. The Headplane default role is `member`, which grants no UI access. Explicit
administrative assignments can use the supported role values through
`properties.headplane_role`; preserve the first-owner bootstrap procedure.

## Start the core services

Restore Casdoor's HTTPS origins and start the proxy using
[public HTTPS setup](deployment.md#13-switch-casdoor-to-public-https), then confirm
OIDC discovery and your chosen provider's callback through the public issuer.
The Feishu callback instruction in that recipe applies only if Feishu is selected.
Both the host and service containers must reach the public issuer.

Follow [Headscale startup and API-key creation](deployment.md#14-start-headscale-and-generate-its-administrative-api-key),
then enroll the designated owner's device using your chosen identity source and
[start Headplane](deployment.md#15-enroll-the-first-device-and-bootstrap-headplane-ownership).
The service commands are:

```sh
stack up -d db casdoor
stack --profile public up -d proxy
stack up -d headscale
# Generate and save .runtime/secrets/headscale_api_key as in the linked procedure.
stack --profile apps up -d headplane
stack ps
```

Ordinary startup does not select `sync`. Do not use `--profile '*' up` for core
startup: that explicitly enables the optional worker. The complete image bundle
still includes the worker so it can be enabled later without a registry download.

Confirm the owner role before expanding access. Enroll an ordinary member, verify
that the member cannot open administration, and allow only a pilot resource in
[network policy](deployment.md#16-permit-one-pilot-resource). Admission groups and
network ACL groups are separate; membership does not automatically grant traffic.

## Optional Feishu integration and acceptance

To use Feishu login and employee data, follow the [Feishu recipe](deployment.md),
[identity contract](identity-contract.md), and [directory-worker guide](../sync/README.md).
Only after configuring, dry-running, and reviewing an applied sync should you run
`stack --profile sync up -d worker`. A Feishu login source alone does not require
running the directory worker; if you omit it, provision users and admission manually
and take responsibility for their lifecycle. Do not schedule overlapping writers.

For any identity source, prove stable login/linking, group removal, disabled-user
handling, existing node/session revocation, role boundaries, and isolated restore.
The release validator retains its `directory_lifecycle` evidence field for schema
compatibility; with no directory adapter, record the tested manual account and
group lifecycle there. Do not mark the field passed merely because Feishu is absent.
Keep `compatibility_verified: false` until the selected deployment passes those gates.
Use [operations](operations.md) for service backups/restores and access revocation;
its Feishu-specific reconciliation steps apply only to that adapter.

## Organization name and logo

After rendering, set these optional values in `.runtime/compose.env`:

```dotenv
HEADPLANE_ORGANIZATION_NAME='Fysics'
HEADPLANE_ORGANIZATION_NAME_EN='Headplane Fysics'
HEADPLANE_ORGANIZATION_NAME_ZH='Headplane 飞捷科思'
HEADPLANE_ORGANIZATION_LOGO_URL='https://assets.example.com/fysics-logo.svg'
```

Replace the example logo URL with your own browser-accessible image URL. The
renderer preserves these settings on subsequent runs. Apply them with
`stack --profile apps up -d --force-recreate headplane`; branding changes do not
require rebuilding the image. The name replaces the header title and browser-tab
title; the logo replaces the H mark. Unset, invalid or unavailable logos use the
original mark. A root-relative image path served by your proxy is also supported;
this setting does not upload or expose a file from the host filesystem.

The optional `_NAME_EN` / `_NAME_ZH` settings select the English/Chinese header and
browser-tab names immediately when language changes. Missing overrides fall back
to `HEADPLANE_ORGANIZATION_NAME`.
