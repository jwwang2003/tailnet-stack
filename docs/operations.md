# Operator handbook

Start with the [core deployment guide](integrated-platform.md) for provider-neutral setup.
The Feishu identity and synchronization sections below apply only when that
optional adapter is enabled; backup, restore, and service operations apply to the core.

This runbook uses the Compose deployment in `deploy/compose.yaml`. It separates
local implementation checks from a live Feishu tenant pilot. The implementation
environment had Python available, but no Docker daemon/client or Go toolchain;
no successful container deployment, live OAuth login, or database restore is
claimed by the local checks.

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

Prepare the sync worker's JSON configuration and secret files according to its
schema. Its `/state` volume is `.runtime/sync/state`, and its `/run/secrets` mount
is `.runtime/secrets`. Review a dry-run report before starting continuous writes.
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
from private files. Restrict Headplane's public route until the designated first
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
failures. A broker outage blocks new logins; existing node access needs separate
network lifecycle handling.

For employee departure, a lost device, or compromised credentials:

1. Remove approved application access or disable the employee in the authoritative
   Feishu scope. Apply and verify synchronization into Casdoor. For urgent action,
   block the Casdoor account and remove admission-group membership immediately;
   ensure the sync worker cannot undo a temporary administrative block before the
   authoritative directory change is in place.
2. Locate every Headscale node and preauth key belonging to that employee. Expire
   the nodes immediately, revoke any preauth keys, and remove approved subnet/exit
   routes for compromised routers. Delete compromised nodes when retaining their
   record is no longer necessary. Authentication disablement alone does not revoke
   enrolled devices or already-issued preauth keys.
3. Revoke Casdoor sessions/tokens through its supported administration controls and
   remove the user's Headplane role. The generated Headplane cookie maximum age is
   300 seconds; existing Headplane sessions can remain valid until expiration.
   For immediate administrative containment, stop Headplane or block its proxy
   route while revocation propagates. Do not promise instant session revocation
   from a directory update.
4. If the employee could access service credentials, rotate those credentials and
   expire affected Headscale API keys. API keys are service credentials, not
   ordinary employee OAuth sessions. Keep the replacement Headplane key private,
   update its mounted file, and restart Headplane. Verify both a fresh login and
   existing device access are denied for the former employee.

The pinned Headscale CLI supports these commands. Replace every example ID with
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
runtime directory except the Headscale Unix socket, a PostgreSQL custom-format
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
the production branch. A ten-user pilot should cover the actual operating systems,
departments, and network environments before onboarding the full team.
