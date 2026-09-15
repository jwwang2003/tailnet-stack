# External Casdoor implementation plan

Planning baseline: Tailnet Stack `1c97bb5`. This is a work assignment and acceptance
plan, not evidence that external mode exists or that a deployment is approved.

## Outcome and scope

Deliver a tested choice between bundled Casdoor and an external HTTPS OIDC issuer.
Headscale and Headplane use separate clients on the shared Casdoor instance.
Installing, stopping, upgrading, backing up, restoring, or removing an external-mode
Tailnet deployment must not manage Casdoor, its database, or Sub2API resources.

Keep bundled installations working. Feishu remains the only implemented directory
adapter; external mode does not imply support for other directory protocols.

This phase includes rendering, service selection, credentials, image delivery,
backup/restore, optional worker ownership, scoped provisioning, and acceptance
records. It excludes live identity migration, a separate identity repository,
Sub2API integration/revocation, new product forks, and production cutover.

Estimated total effort: 3–5 engineering days including integration and staging,
assuming an available test issuer and Docker host. Parallelism reduces elapsed
implementation time, not the need for integration and recovery testing. Live
migration is a separate estimated 2–4 day phase, to be re-estimated after inventory.

## Current coupling to remove

- `scripts/configure.py` requires all three product images, generates a Casdoor
  database password/configuration, and derives the issuer from `casdoor_host`.
- `deploy/compose.yaml` includes local `db` and `casdoor`; `deploy/Caddyfile`
  proxies the login domain to the local Casdoor container.
- Backup always stops Casdoor and dumps PostgreSQL. Restore requires that dump
  and starts a new Casdoor database. Both select the bundled Compose definition.
- `scripts/release.py`, `scripts/image-bundle.py`, and build helpers assume the
  bundled product/support image set. The SWR CLI discovers images but does not
  yet select them by deployment ownership.
- `scripts/casdoor-harden.py` defaults to all interactive applications in an
  organization. Even with `--application`, it plans organization changes.
- Worker API credentials have broad Casdoor management privileges; an organization
  setting is not a server-enforced least-privilege boundary.

## Contract to freeze before delegation

The coordinator owns this contract and a small shared deployment-selection module,
proposed as `scripts/deployment.py`, with its focused tests. Other agents consume
it rather than implementing independent mode rules. Freeze the actual function
signatures, schema fixtures, and invocation examples in a contract commit before
parallel implementation.

### Site settings

Add the following to the existing site JSON; existing hostname, organization,
admission group, and client ID fields remain supported:

```json
{
  "identity": {
    "mode": "external",
    "issuer": "https://login.example.com"
  },
  "directory_sync": {
    "owner": "external"
  }
}
```

- Missing `identity` means legacy bundled behavior. External mode requires an
  explicit HTTPS issuer. Validate URL syntax and reject credentials, query, and
  fragment. Preserve meaningful issuer paths and exact discovery issuer matching.
- Bundled mode continues deriving its issuer from `casdoor_host`; conflicting
  explicit issuer settings fail rather than silently selecting one.
- Directory ownership is `local`, `external`, or `disabled`. New external-mode
  sites default to `external`. Legacy sites retain their optional local worker
  behavior. Local ownership permits worker startup but does not start it merely
  because configuration was rendered.
- `external` and `disabled` omit the local worker and its credentials from the
  selected deployment. `disabled` means no synchronizer is declared; it does not
  revoke existing accounts or group memberships.
- External mode consumes existing Headscale/Headplane client secret files via
  explicit renderer arguments. Require separate nonempty credentials on first
  setup, preserve existing values on rerender, and never invent a secret that has
  not been registered at the issuer. Keep credentials out of site JSON and output.
- Rendering is configuration-only: no identity API writes, container operations,
  credential rotation, or deletion of former bundled state.

### Service and artifact selection

Freeze a deterministic selection function shared by runtime and release tooling:

| Mode | Identity services | Tailnet services | Worker |
| --- | --- | --- | --- |
| Bundled, legacy defaults | Casdoor + PostgreSQL | Headscale, optional Headplane, public proxy | Optional local worker |
| External + external/disabled sync | None | Headscale, optional Headplane, public proxy | None |
| External + local sync | None | Headscale, optional Headplane, public proxy | Explicitly configured local worker |

Persist an explicit deployment descriptor (proposed `runtime/deployment.json`)
containing schema version, mode, exact issuer, directory ownership, selected
services/artifacts, and configuration checksums. No credentials. Missing metadata
on historical runtimes means legacy bundled mode; invalid or contradictory
metadata fails before side effects. Existing runtimes cannot silently change
mode or issuer on rerender: require a separate reviewed migration.

Generate an effective runtime Compose file and Caddyfile from maintained source
templates, selected by the descriptor. Keep the old bundled invocation working
for legacy runtimes. External Compose must physically omit `db`, `casdoor`, their
secrets/volumes, and the login-host proxy route. Profiles alone are insufficient:
`--profile '*'` must not resurrect external identity services. The offline overlay
must target only selected services and must not reintroduce excluded services.
Use the same effective Compose files for startup, backup, restore, and teardown.
Avoid two independently maintained copies of the entire service definition.

Keep product source pins as a catalog distinct from deployed image ownership.
External mode can retain a known compatible Casdoor revision as reference metadata
without requiring its checkout or image locally. Release records must explicitly
identify the external issuer and its tested version/identity release evidence;
omitted Casdoor artifacts must never imply validated external compatibility.

New bundle/backup metadata must include deployment mode and selected artifacts.
Use a new schema version where required; continue reading existing v1 bundled
archives under their original six-image/database rules. Never rewrite RC4 release
refs, manifests, image identities, or historical archive checksums.

## Agent execution model

Use at most four concurrent agents: coordinator plus three workers. Each worker
gets an isolated worktree from the same contract commit and owns only its listed
files. Agents may read the whole repository. They do not edit shared files or
another worktree; send proposed cross-boundary changes to the coordinator.

### Wave 0 — coordinator: contracts and fixtures (about half a day)

1. Verify clean base and applicable repository instructions.
2. Freeze settings, descriptor/backup/bundle schemas, service selection, credential
   arguments, and backward-compatibility examples above.
3. Implement the small pure shared contract module, with valid/invalid fixtures
   and tests. No live I/O in selection functions.
4. Specify effective runtime Compose paths and how each CLI selects a runtime.
   Verify required Docker Compose capabilities on the acceptance host.
5. Commit the contract and create worktrees for A, B, and C from that commit.

Coordinator file ownership: `scripts/deployment.py`, its test/fixtures, this plan,
shared documentation, CI, `pyproject.toml`, `uv.lock`, and requirements exports.
Contract changes after dispatch require notifying every affected worker and
updating their base; no silent schema drift.

### Wave 1A — agent `external-config`: rendering and service topology

Own: `scripts/configure.py`, `deploy/compose.yaml`, `deploy/compose.offline.yaml`,
`deploy/Caddyfile`, `deploy/site.example.json`, `tests/runtime/test_configure.py`.
Own any agreed rendering templates under `deploy/`; coordinate filenames in Wave 0.

Deliver:

- Bundled/external renderer and descriptor generation using the shared contract.
- Effective Compose/proxy configuration with excluded identity resources absent.
- Existing-client-secret handling and preserved rerender state.
- Example external site settings and exact startup commands for documentation.

Acceptance:

- Legacy bundled fixtures still render with preserved project names, image pins,
  policy, and secrets.
- External output contains the right issuer/client IDs and no local database
  password, Casdoor config, identity volume, or login-domain route.
- Both selected Compose definitions pass `docker compose config --quiet`.
- Inspect resolved services with all profiles enabled; external mode still cannot
  create Casdoor/PostgreSQL or an externally owned worker.
- Invalid settings, missing credentials, and unintended mode/issuer changes fail
  before partial runtime writes.

### Wave 1B — agent `external-recovery`: backup and restore boundaries

Own: `scripts/backup.sh`, `scripts/restore.sh`,
`tests/release/test_backup_restore.py`. Consume descriptor fixtures from Wave 0;
do not wait for A's completed renderer to start unit tests.

Deliver:

- Descriptor-aware effective Compose selection and owned-service stop/restart.
- External archives containing Tailnet state, credentials, descriptor and selected
  deployment files; no Casdoor dump or identity-owned data.
- External restore that does not execute `pg_dump`, `pg_restore`, create a database,
  or start application writers. Bundled behavior remains supported.
- Mode-aware validation before extraction/resource creation, plus v1 support.

Acceptance:

- Mock command traces prove no identity service or API calls in external mode.
- Preserve Headscale private keys/database, Headplane session state, proxy state,
  and local worker state/journal when selected.
- Preserve no-overwrite, path traversal/symlink rejection, checksums, maintenance
  locking, correct runtime rebasing, and restart-on-failure behavior.
- Reject descriptor/archive/expected-mode mismatch before side effects.
- Restore leaves the worker stopped; a clone must not reconcile real employees.
- Archive external identity dependency/recovery references without pretending to
  back up or restore the shared identity service.

### Wave 1C — agent `external-release`: images and promotion

Own: `scripts/release.py`, `scripts/image-bundle.py`, `scripts/build-products.sh`,
`scripts/huawei-swr.py`, `releases/manifest.example.yaml`, relevant tests under
`tests/release/test_release.py`, `tests/release/test_huawei_swr.py`,
`tests/bundle/test_image_bundle.py`, and `tests/build/test_build_proxy.py`.

Deliver:

- One selected artifact set consumed by verification, build, export/import, SWR
  default uploads, and promotion. Preserve explicit SWR `--image` behavior.
- No local Casdoor source/image or PostgreSQL requirement in external mode;
  omit worker images when directory synchronization is externally owned/disabled.
- Mode/artifact binding in new bundles and promotion inventories.
- Mode-aware evidence requirements, with explicit reasons for inapplicable local
  identity migrations/backups and separate external issuer compatibility evidence.

Acceptance:

- Bundled v1 bundles still validate strictly; new external bundles validate only
  their declared, expected artifact set, rejecting missing/extra/substituted images.
- Verify architecture, source revisions, image digests, integration commit and
  lock/configuration binding; never bypass checks to permit a smaller bundle.
- Missing external OIDC evidence still blocks promotion.
- Build/upload commands select the same set as runtime rendering.
- Existing arbitrary-image SWR CLI tests continue passing.

### Wave 2 — integration, shared identity boundaries, and independent review

Coordinator integrates A, then B and C, resolving cross-file conflicts and running
`uv run --locked bash scripts/test.sh`. A worker may assist only after receiving a
new explicit file assignment. Do not merge unverified agent results.

Assign the next available worker `identity-boundaries`:

Own: `scripts/casdoor-harden.py`, `scripts/verify-casdoor-authz.py`, and their tests.

- External-mode hardening must require explicit applications and must skip
  organization-wide mutations unless separately selected as an identity-owner task.
- Reject missing/unknown requested applications; do not report a successful no-op.
- Scope probes to synthetic fixtures and explicit intended applications. Never
  run a mutating probe against the shared live issuer during implementation.
- Verify local-worker external mode can reach the configured Casdoor API and
  preserves native syncer verification, stable IDs and durable revocation behavior.
  Any worker changes require a new bounded assignment with its own tests.
- Document that client credentials can still be broadly privileged; CLI scoping
  is a guardrail, not tenant isolation. Externally owned sync is the default.

Assign a different available worker `independent-review` after integration:

Read-only review of the combined diff, focusing on identity resource deletion,
credential replacement, issuer changes, duplicated workers, mode mismatch and
weakened archive/promotion checks. Reproduce actionable findings with tests or
precise command traces. Authoring agents fix their assigned files; reviewer
rechecks fixes. No review approval based only on passing unit tests.

Coordinator writes the external deployment and recovery guide, updates README,
existing instructions, and CI. All entry points must select the same runtime;
remove examples that accidentally use the bundled Compose file in external mode.

### Wave 3 — isolated acceptance and release candidate

Run against synthetic accounts and separate disposable projects/volumes:

1. Start an independently managed Casdoor test stack with the compatible patched
   image, then deploy external-mode Headscale/Headplane against it.
2. Verify DNS/TLS and discovery from clients and service containers. Complete both
   OIDC login flows and confirm stable subject linking, correct groups and roles.
3. Reject wrong issuer/audience, invalid tokens, and denied users. Test issuer
   outage/recovery without fallback authentication or unintended provisioning.
4. Stop and remove only the Tailnet test deployment. Check issuer availability,
   identity database contents and a second synthetic OIDC client before/after.
5. Back up and restore external mode into a fresh isolated deployment; compare
   persistent state and complete login again. No shared database operations.
6. Repeat bundled deployment/recovery, plus legacy archive validation. Verify the
   bundled option has not regressed.
7. Exercise external mode with local sync using synthetic employees, proving
   revocation/journal persistence and that restore does not start a duplicate worker.
8. Validate registry and offline delivery for the actual target architecture,
   including ARM64 if that remains the deployment host. CLI previews are not proof
   that images run on the target architecture.
9. Publish a new candidate with exact source/image/configuration identities and
   observed evidence. Do not relabel RC4 artifacts or mark compatibility verified
   while mandatory live checks remain pending.

Use a minimal second OIDC client for ownership-isolation checks. This does not
claim full Sub2API compatibility or revoke Sub2API sessions/API keys.

## Required handoff from every agent

Return the base SHA and result SHA(s), owned files changed, contract assumptions,
commands and results, unresolved findings, acceptance coverage, and rollback note.
Keep commits cohesive and independently reviewable. New Python CLI work follows
existing Typer/Rich and uv conventions; use `#!/usr/bin/env python` for new Python
scripts. Do not mix unrelated repository-wide formatting into feature commits.

An agent task must state: objective, exact file ownership, shared contract commit,
acceptance checks, dependencies, and forbidden live mutations. Completion means
implementation plus tests, not a partial plan. API/host-dependent checks that cannot
run are reported explicitly; fixture success is not substituted for live evidence.

## Completion criteria and live migration follow-up

External mode is complete when it operates and recovers independently of identity
infrastructure; both login flows pass; all delivery paths honor the same selected
resources; scoped tooling cannot incidentally modify unrelated applications; and
bundled deployments plus historical archives remain supported.

After that candidate is reviewed, schedule live identity extraction separately:
inventory the current issuer URL, users/subjects, signing keys, app clients,
database/files, and worker ownership; take a tested backup; preserve the issuer
and identifiers; rehearse extraction; arrange a controlled cutover and rollback;
then verify every consuming service. Never automate mode switching or remove the
old identity volumes as part of the first external-mode implementation.
