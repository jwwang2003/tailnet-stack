# Operator handbook

For external identity deployments, use the [external Casdoor runbook](external-casdoor.md)
and runtime-generated Compose files. Casdoor/database recovery belongs to the
identity operator. The repository-level Compose commands below describe bundled
installations and must not be reused unchanged for external mode.

Start with the [core deployment guide](integrated-platform.md) for provider-neutral setup.
The Feishu identity and synchronization sections below apply only when that
optional adapter is enabled; backup, restore, and service operations apply to the core.

This runbook uses `deploy/compose.yaml` with Docker Compose v2 semantics. The
ARM64 candidate passes 188 integration tests, native builds, bundle verification
and a Docker-host synthetic restore rehearsal. Full tenant lifecycle, public OIDC
and device/session tests remain pending. See the [current review](production-readiness.md).

## Prepare a deployment host

Use a Linux host with Docker Engine and Docker Compose, Git, Python 3, GNU tar,
`flock`, and enough persistent disk for service state plus a complete local backup.
Install the build helper's Python requirements in a virtual environment. Assign
separate public DNS names for Casdoor, Headscale, and Headplane and a distinct
tailnet DNS suffix. Allow inbound HTTPS; HTTP is needed by the default proxy's
certificate automation. The default configuration uses Tailscale's published DERP
map; check connectivity from employees' actual networks before selecting a private
DERP deployment. Do not expose PostgreSQL or metrics to the public network.

Place the integration repository beside the three service forks. Check out the
candidate source commits from `versions.lock.yaml`, including the patched Casdoor
commit. Follow [build and versioning](build-versioning.md); stock Casdoor is not a
substitute for the downstream identity changes. Record resulting image digests
before production deployment. The default local tags and proxy/database tags are
candidate build inputs, not immutable production pins.

Copy `deploy/site.example.json` to a private `site.local.json`, then replace its
hostnames, organization, client IDs, and admission group. Generate configuration:

```sh
python3 scripts/configure.py --site site.local.json --output .runtime
```

The renderer creates private configuration and secrets, preserves existing secret
values and network policy, and records the absolute runtime path in
`.runtime/compose.env`. Keep runtime directories owned by the operator UID/GID used
by the service containers. Do not move a live runtime directory without stopping
services and regenerating its paths. Replace image values in `compose.env` with
the tested registry digest references for production.

For the commands below, run Bash from the integration checkout and define:

```sh
runtime=$(realpath .runtime)
dc=(docker compose --env-file "$runtime/compose.env" --file deploy/compose.yaml --profile '*')
"${dc[@]}" config --quiet
```

## Establish Feishu and Casdoor identity

Create a Feishu App with authorization-code login and the required Contact API
permissions. An app developer can configure the integration, but directory access
still depends on approved app permissions and the tenant's permitted data range.
Capture which departments and Feishu user groups are in scope; a scope reduction
must not be interpreted as employees leaving the company. Use the exact Feishu app
for both OAuth and synchronization so its app-scoped `open_id` values agree.

Start the database and Casdoor on the host loopback interface:

```sh
"${dc[@]}" up --detach db casdoor
"${dc[@]}" logs --tail 100 casdoor
```

Reach the Casdoor bootstrap page locally or through an SSH tunnel to host port
8000. Replace bootstrap administrator credentials before exposing its public DNS
name. Configure the Lark/Feishu OAuth provider, tenant organization, signing key,
and two downstream applications using the verified identity contract. Match the
application client IDs in `site.local.json` and the generated OIDC client secrets;
transfer secret values through the Casdoor administrative interface without
putting them into shell history or Git.

Set exact redirects:

```text
Headscale: https://HEADSCALE_HOST/oidc/callback
Headplane: https://HEADPLANE_HOST/admin/oidc/callback
```

Casdoor's Feishu callback must separately match the Feishu App configuration.
Enable only the tested external provider on employee-facing applications and
disable unrestricted local sign-up. Keep the emergency administrative identity
separate from employee synchronization. Verify that both OIDC applications emit
the same stable subject for the employee and that the signed group claim contains
the configured `organization/admission_group` entry. An employee's email must be
verified through the selected trusted source; do not bypass Headscale's verified
email requirement merely to make a test login succeed.

After creating the applications, enforce the account policy with the hardening
script, first as a dry run and then with `--apply`. It runs on the host and needs
a small configuration with the Casdoor loopback origin and the service secret
path (see the [Feishu deployment guide](deployment.md), step 11):

```sh
python3 scripts/casdoor-harden.py --config .runtime/casdoor-host.json
python3 scripts/casdoor-harden.py --config .runtime/casdoor-host.json --apply
```

It makes directory-managed account items Admin-only, keeps password, language,
and MFA self-service, sets every provider's binding rule to `[]` so an unlinked
Feishu login is refused rather than attached to an existing account by email,
phone, or username, and disables Face ID, WebAuthn, verification-code sign-in,
and sign-up. Rerun it after any organization or application edit in the Casdoor
UI, which can reset the binding rule to null.

Prepare the sync worker's JSON configuration and secret files according to its
schema. Its `/state` volume is `.runtime/sync/state`, and its `/run/secrets` mount
is `.runtime/secrets`. Run `--permissions` first: it lists the missing Feishu
scopes per feature and a `grant_url` to request them. Choose `lifecycle_mode:
"staged"` to import users while the status scope is pending (admission, roles,
and offboarding stay skipped until it is granted, then start automatically) or
`strict` to refuse such runs. Add the `headscale` section so that blocking an
account also expires the employee's nodes and preauth keys. Review a dry-run
report before starting continuous writes.
Do not run Casdoor's native Lark syncer concurrently against the same population
unless the identity contract explicitly assigns field ownership to both writers.

## Start and validate the network services

Start the proxy only after bootstrap hardening, then Headscale:

```sh
"${dc[@]}" up --detach proxy headscale
"${dc[@]}" exec headscale headscale health
"${dc[@]}" exec headscale headscale apikeys create --expiration 90d
```

Store the generated Headscale API key in `.runtime/secrets/headscale_api_key` with
mode 600 and start Headplane. Its OIDC secret and cookie secret are already mounted
from private files. The worker's optional `headscale` section reads the same API
key file; after a rotation replace the file and restart both Headplane and the
worker. Restrict Headplane's public route until the designated first
owner completes OIDC login, because first-user owner bootstrap is an administrative
event. The default OIDC role is `member`; elevated roles must be explicit and
reviewed.

```sh
"${dc[@]}" up --detach headplane
"${dc[@]}" ps
"${dc[@]}" logs --tail 100 headscale headplane
```

Run one employee through Feishu login, node registration, and Headplane identity
linking. The initial Headscale ACL file denies traffic. Add narrowly scoped rules
for approved services to `.runtime/headscale/policy.json` and verify them with the
running Headscale version before expanding access. Feishu/Casdoor admission groups
control who may authenticate; they do not automatically populate ACL groups or
grant access between devices.

Enable the continuous worker only after its initial dry-run and apply report have
been reviewed:

```sh
"${dc[@]}" up --detach worker
"${dc[@]}" logs --tail 100 worker
```

## Routine operations and offboarding

Monitor service health, OAuth failures, successful full-sync age, partial-scope
errors, missing membership reports, database size, backup age, and TLS renewal.
Alert if no complete sync succeeds within two expected polling intervals; retain
the previous membership state while investigating partial API or permission
failures. The worker writes `sync-state.json.heartbeat.json` in `.runtime/sync/state`
after every successful applied interval, and its Compose healthcheck reports
unhealthy when no success occurred within three intervals. A staged run
(`lifecycle: skipped`) counts as a success there, so alert on `lifecycle`
separately while admission must be live. A broker outage blocks new logins;
existing node access is revoked by the worker only for blocked accounts and only
when `headscale` is configured, and otherwise needs separate network lifecycle
handling.

For employee departure, a lost device, or compromised credentials, the worker
covers the common path when its `headscale` section is configured: a full applied
run that blocks a Casdoor account (inactive Feishu status, or a user confirmed
missing for `missing_confirmations` runs) also expires, or deletes per
`on_block`, every Headscale node registered by that OIDC subject and expires the
subject's preauth keys; the report shows the counts under `revocation`. The
subject is recorded in the journal `sync-state.json.revocations.json` before the
block is sent, and the Casdoor user receives `properties.feishu_sync_revoked`
once Headscale has confirmed. If Headscale is unreachable, or the run fails or
crashes after the block, the block stays applied, the journal entry is kept, the
run ends with an error, and the revocation is retried every interval, including
staged runs, until it succeeds; every applied run also revokes any linked user
that is forbidden with a worker marker but no revoked marker, and a new block
clears a stale revoked marker left by an earlier offboarding. Do not edit the
journal by hand; a corrupt journal stops synchronization until it is restored
from backup.

For immediate containment, before the directory change reaches the worker:

```sh
"${dc[@]}" run --rm worker --config /config/sync.json --offboard employees/EMPLOYEE_NAME
"${dc[@]}" run --rm worker --config /config/sync.json --offboard employees/EMPLOYEE_NAME --apply
```

The dry run lists the Casdoor changes. `--apply` first takes the worker's state
lock, waiting up to `lock_wait_seconds` (default 300) for a running interval to
finish, then records the subject in the revocation journal, blocks the account,
sets `properties.feishu_sync_hold: "true"` so no later run re-enables it, sets
`headplane_role` to `member`, removes the admission alias and `feishu-*` groups
while keeping local groups, and, when `headscale` is configured, expires the
employee's nodes and preauth keys at once and marks the user
`feishu_sync_revoked`. If the command fails after the block because Headscale is
unreachable, the journal entry remains and the next scheduled applied run
completes the revocation; confirm it in that run's `revocation` counters, or
revoke manually below if it cannot wait. Clear the hold only after the review
that would justify re-enabling.

Then complete and verify manually; these steps are also the full procedure when
`headscale` is not configured:

1. Remove approved application access or disable the employee in the authoritative
   Feishu scope so the next full run confirms the block. The hold marker set by
   `--offboard` prevents the worker from undoing the block before the directory
   change is in place; the worker also preserves an administrative block that
   carries no worker marker.
2. List every Headscale node and preauth key belonging to that employee and
   confirm they are expired or deleted. Without `headscale`, expire the nodes
   now and revoke any preauth keys. In both cases remove approved subnet/exit
   routes for compromised routers; the worker never edits routes. Delete
   compromised nodes when retaining their record is no longer necessary.
   Authentication disablement alone does not revoke enrolled devices or
   already-issued preauth keys.
3. Revoke Casdoor sessions/tokens through its supported administration controls.
   The generated Headplane cookie maximum age is 300 seconds; the worker does not
   revoke Headplane sessions, so existing sessions can remain valid until
   expiration. For immediate administrative containment, stop Headplane or block
   its proxy route while revocation propagates. Do not promise instant session
   revocation from a directory update.
4. If the employee could access service credentials, rotate those credentials and
   expire affected Headscale API keys. API keys are service credentials, not
   ordinary employee OAuth sessions. Keep the replacement key private, update its
   mounted file, and restart Headplane and the worker, which share it. Verify
   both a fresh login and existing device access are denied for the former
   employee.

The pinned Headscale CLI supports these commands for verification and for manual
revocation. Replace every example ID with
the verified result of the corresponding list command; list output can contain
credential material and must not be pasted into public tickets:

```sh
"${dc[@]}" exec headscale headscale users list
"${dc[@]}" exec headscale headscale nodes list --user EMPLOYEE_NAME
"${dc[@]}" exec headscale headscale nodes expire --identifier NODE_ID
"${dc[@]}" exec headscale headscale nodes approve-routes --identifier NODE_ID --routes ''
"${dc[@]}" exec headscale headscale preauthkeys list
"${dc[@]}" exec headscale headscale preauthkeys expire --id PREAUTH_KEY_ID
"${dc[@]}" exec headscale headscale apikeys list
"${dc[@]}" exec headscale headscale apikeys expire --id API_KEY_ID
"${dc[@]}" exec headscale headscale nodes delete --identifier NODE_ID
```

## Backup and restore

Run backups from the same integration release checkout used by the active stack:

```sh
mkdir -m 700 -p /srv/tailnet-backups
bash scripts/backup.sh .runtime /srv/tailnet-backups/tailnet-2026-09-14.tar.gz
```

The helper refuses an existing output file and stops worker, Headplane, Headscale,
Casdoor, and proxy while retaining PostgreSQL for `pg_dump`. It captures the entire
runtime directory, including Casdoor's `casdoor/files` uploads and the worker
state, except the Headscale Unix socket, a PostgreSQL custom-format
dump, Caddy data/configuration volumes, the release lock, and the deployed proxy/
Compose configuration. It restarts only services that were originally running,
including after a failed backup attempt. Inspect restart failures immediately.

The archive and temporary staging directory are protected with mode 600 and 700,
respectively. The archive includes credentials and private keys and is not
encrypted by the script. Encrypt it using the organization's backup system before
off-host storage. Save its printed SHA-256 in a separate trusted inventory and
verify it before restoration; a matching source lock is not a signature proving
that an archive is authentic. Retain the corresponding image digests and source
checkout alongside the backup inventory. Retain `site.local.json` separately in
that protected inventory if it lives outside the runtime directory; the archive
contains rendered service configuration but does not search the host for site files.

Both helpers require Docker Compose v2 semantics (`ps --services --status`, `cp`,
`create`, and the literal `--profile '*'`). Unmodified podman-compose stops
`backup.sh` before any service is touched and stops `restore.sh` after the
database import; run rehearsals on a Docker Engine host, or provide those
subcommands through a shim. The 2026-09-14 rehearsal record is in
`releases/2026.09-rc.2-validation.md`.

Restore first on an isolated host with no public DNS traffic. Supply the exact
release lock retained with the backup and a new directory/project name:

```sh
bash scripts/restore.sh /srv/tailnet-backups/tailnet-2026-09-14.tar.gz versions.lock.yaml /srv/tailnet-restore-20260914 restore-20260914
```

Restore refuses an existing directory, existing project containers, existing
database/Caddy volumes, unsafe tar paths or links, and mismatched source-lock or
database checksums. It rewrites the runtime absolute path, operator UID/GID, and
Compose project name. It starts only the new PostgreSQL instance, imports the
dump, and restores Caddy state into a stopped proxy container. It does not delete
any existing database or start application writers. A failed restore leaves its
new directory/project available for inspection; use a different fresh destination
for a retry after diagnosing the failure.

Use the restored project and files for every follow-up command:

```sh
restored=/srv/tailnet-restore-20260914
rdc=(docker compose --project-name restore-20260914 --env-file "$restored/runtime/compose.env" --file "$restored/deploy/compose.yaml" --profile '*')
"${rdc[@]}" up --detach casdoor headscale
```

Before exposing proxy ports or resuming synchronization, verify source/image pins,
Casdoor issuer and signing keys, existing node connectivity, new Feishu login,
Headplane sessions, and the worker's identity mappings. Keep the worker stopped
until its restored state agrees with the restored Casdoor database. Record the
restore test and recovery duration. For rollback after a database migration,
restore the pre-migration backup with the old images; never start an old Headscale
binary against a newer migrated database.

## Release acceptance

Complete [the acceptance checklist](acceptance-checklist.md), record evidence in
the release manifest, and run the promotion checker before moving a candidate to
the production branch. Run `scripts/verify-casdoor-authz.py --config
.runtime/casdoor-host.json` against the candidate Casdoor image; it must exit 0,
proving that an ordinary employee session cannot change properties, groups,
`isAdmin`, `isForbidden`, or email. A ten-user pilot should cover the actual operating systems,
departments, and network environments before onboarding the full team.
