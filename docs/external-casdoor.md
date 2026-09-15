# Use an external Casdoor instance

External mode lets Headscale and Headplane use an independently managed HTTPS
Casdoor issuer. Casdoor, its database, and other clients such as Sub2API remain
outside this deployment's service lifecycle. Feishu is still the only implemented
directory adapter; external mode is not a new directory synchronization protocol.

Use a new runtime directory for this mode. Switching an existing bundled runtime
is a migration, not a renderer option. Preserve its issuer, users/subjects, signing
keys, client registrations, and database before planning any extraction. Do not
remove old identity volumes or rerender an active deployment to migrate it.

## Prepare the identity service

The shared identity operator owns Casdoor's DNS/TLS, signing keys, upgrades,
backup/restore, and availability. Keep the issuer available independently of
Tailnet Stack; browsers and both service containers must reach its HTTPS URL.
Use a supported Casdoor version and verify the actual issuer/discovery response.
The source catalog's Casdoor revision is compatibility context, not proof that
an arbitrary external server behaves the same way.

Create separate confidential Authorization Code applications for Headscale and
Headplane, enable S256 PKCE, and configure these exact callbacks with your domains:

| Client | Callback |
| --- | --- |
| Headscale | `https://vpn.example.com/oidc/callback` |
| Headplane | `https://admin.example.com/admin/oidc/callback` |

Use `openid profile email groups`, a stable immutable subject, verified-email
claims, and the reviewed admission group (`employees/tailnet-members` by default).
Preserve Headplane's `member` default and assign operator roles explicitly.
Configure application/provider policy through the shared identity operator; the
Tailnet renderer does not provision or harden the external server.

Store the two registered client secrets in separate private files outside Git.
The renderer consumes them; it must not create replacement secrets that Casdoor
has never registered. Do not reuse Sub2API's client credentials.

## Render a new runtime

From the integration checkout:

```sh
uv sync --locked
```

Prepare a private site file with your real hosts and client IDs:

```json
{
  "identity": {
    "mode": "external",
    "issuer": "https://login.example.com"
  },
  "directory_sync": {"owner": "external"},
  "headscale_host": "vpn.example.com",
  "headplane_host": "admin.example.com",
  "tailnet_domain": "tail.example.net",
  "organization": "employees",
  "admission_group": "tailnet-members",
  "headscale_client_id": "headscale",
  "headplane_client_id": "headplane"
}
```

Render using the actual secret files:

```sh
uv run python scripts/configure.py \
  --site /private/site-external.json \
  --output .runtime/external \
  --headscale-client-secret-file /private/headscale-client-secret \
  --headplane-client-secret-file /private/headplane-client-secret
```

The runtime contains `deployment.json`, `compose.env`, application settings,
private client secrets, and `deploy/compose.yaml`, `deploy/compose.offline.yaml`,
`deploy/Caddyfile`. The descriptor records exact identity ownership and the
selected deployment-file checksums. It excludes secrets and mutable policy from
its checksum map. Rerender preserves existing credentials, policy and image pins;
changing mode or issuer requires an explicit migration outside this workflow.

External Compose physically omits Casdoor, PostgreSQL, their volumes/secrets,
and the login-host reverse-proxy route. With externally owned synchronization it
also omits the local worker. Enabling all profiles cannot reintroduce them.

## Select and start the deployment

Always use this runtime's generated files. The repository-level
`deploy/compose.yaml` remains the legacy bundled deployment, not an external-mode
entry point.

```sh
stack() {
  docker compose --env-file .runtime/external/compose.env \
    -f .runtime/external/deploy/compose.yaml "$@"
}
stack --profile '*' config --quiet
stack --profile '*' config --services
```

The services should be `headscale`, `headplane`, and `proxy` for the example
above. Configure exact reviewed image pins before startup. Check issuer discovery
from the server and service network without disabling TLS verification. Start
Headscale, create its administrative API key in the private runtime secret file
using the existing deployment procedure, then start Headplane and the public
proxy. Restrict first-owner bootstrap to the designated operator.

For offline delivery, add the generated runtime offline overlay after the runtime
Compose file. Do not use the repository-level six-service overlay:

```sh
docker compose --env-file .runtime/external/compose.env \
  -f .runtime/external/deploy/compose.yaml \
  -f .runtime/external/deploy/compose.offline.yaml \
  --profile '*' config --quiet
```

Image and release commands accept `--deployment .runtime/external/deployment.json`.
The product build helper accepts `DEPLOYMENT_FILE` pointing at that descriptor.
Use the same descriptor for verification, export/import and default SWR upload
selection. Without a descriptor, legacy build/release commands retain bundled
behavior. SWR's explicit `--image` selection remains an independent custom upload.

```sh
uv run python scripts/release.py --deployment .runtime/external/deployment.json artifacts
uv run python scripts/release.py --deployment .runtime/external/deployment.json verify-sources ..
uv run python scripts/huawei-swr.py --deployment .runtime/external/deployment.json \
  --region cn-east-3 --organization YOUR_ORGANIZATION --dry-run
```

`release.py` takes the descriptor before its subcommand; `image-bundle.py` takes
it after `export`, `import`, or `check`. Import also detects the destination
runtime descriptor and refuses a contradictory explicitly selected descriptor.

New external bundles bind the selected artifact set and deployment descriptor;
v1 bundles retain their original bundled rules. Do not edit an existing bundle's
metadata to remove Casdoor. Re-export a new candidate from the correct source and
mode instead. Catalog references to Casdoor do not require building/pulling it in
external mode.

## Directory ownership and hardening

`directory_sync.owner` has three values:

- `external`: another deployment owns synchronization; no local worker is included.
- `disabled`: no synchronizer is declared here; existing memberships are unchanged.
- `local`: include the optional worker, with explicit API credentials and a reviewed
  worker configuration. Configure only one active synchronizer for the organization.

Headscale needs admission groups whether they come from a synchronizer or reviewed
manual provisioning. External mode does not create those groups. Existing node and
session revocation needs its own owner; OIDC login alone does not revoke established
access, nor does the Headscale adapter revoke Sub2API sessions or API keys.

For application-only hardening, supply the descriptor and exact applications:

```sh
uv run python scripts/casdoor-harden.py \
  --config /private/identity-api-config.json \
  --deployment .runtime/external/deployment.json \
  --application admin/app-headscale \
  --application admin/app-headplane
```

This previews changes. `--apply` writes the reviewed application changes.
For configs inside the runtime or its `sync/` directory, the tools also detect
the adjacent runtime descriptor; omitting `--deployment` cannot bypass external
scope restrictions. Keep the descriptor with its runtime. For API configs stored
elsewhere, supply `--deployment` explicitly.
Organization-wide policy is excluded unless `--include-organization` is explicitly
selected by the shared identity operator. The option affects all users sharing the
organization and is not needed merely to connect an OIDC client.

The authorization probe creates synthetic resources. External mode requires both
`--application OWNER/NAME` as its template and `--fixture-organization` matching
the API config's organization. Run it in an isolated test issuer/organization.
These CLI guards do not narrow Casdoor's broad API credential privileges; protect
and separately manage provisioning credentials.

## Backup, restore and acceptance

Use `scripts/backup.sh RUNTIME NEW_ARCHIVE` with the external runtime directory.
It backs up only Tailnet-owned state and stops/restarts only owned local writers.
It does not dump or stop the external Casdoor database. Keep the shared identity
service's recovery evidence and backups in its own operations inventory.

Restore to a new directory/project using the original expected source lock and
the archive's selected mode. External restore must leave application writers and
workers stopped; verify the restored issuer and client registrations before startup.
A restored copy must never become a second active directory synchronizer.
Legacy bundled archives keep their existing database restore procedure.

```sh
uv run bash scripts/backup.sh .runtime/external /private/backups/tailnet-external.tar.gz
uv run bash scripts/restore.sh /private/backups/tailnet-external.tar.gz \
  /private/inventory/versions.lock.yaml .runtime/external-restored \
  tailnet-external-restored --expected-mode external
```

Use a previously nonexistent archive path and restore directory, and retain the
exact expected lock from the backup's release. Restored runtime files live under
the destination's `runtime/`; use that directory's `compose.env` and generated
`deploy/` files, with the restore project name, on every subsequent operation.

For promotion, the manifest must include `deployment`, `deployment_sha256`,
`external_identity: {issuer, version, evidence, recovery_reference}`, and reasoned
`not_applicable` entries for `casdoor_migration` and `casdoor_database_backup`.
Tailnet OIDC, access lifecycle/revocation and isolated recovery evidence remain
mandatory. New schema-2 bundles need the project Python dependencies; historical
schema-1 bundle validation retains the legacy dependency and artifact rules.

Before promotion, record both successful OIDC login flows, subject/group/role
checks, issuer outage/recovery, and Tailnet-only backup/restore. Demonstrate that
stopping/removing the Tailnet test project leaves the shared identity service and
a second OIDC client available. Record the external issuer's tested version and
identity-release/recovery evidence. These checks are separate from unit tests and
from uploading compatible image tags.
