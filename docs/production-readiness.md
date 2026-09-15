# Release review and deployment sequence

## Gate execution follow-up

The subsequent sub-agent audit found that the documented deployment server is
ARM64, while the original RC3 bundle is amd64. Native candidate integration
`292f2aa` pins Casdoor `9d982e2d` and Headplane `a0e18d8`; Headscale is unchanged.
The original amd64 bundle is retained. **Production acceptance remains pending.**

- Fresh tenant reads: 207 employees, 45 departments, zero complete employment
  status records. Both requested Feishu scope grants are still missing.
- Corrected the Casdoor hardener's unsupported Face ID field. Applied the
  supported sign-in-method settings in the sandbox; recheck reports zero
  pending changes and all eight ordinary-user authorization probes pass.
- Added server-side WebAuthn application/user checks before session creation.
  Five tests with 49 subcases pass, including genuine synthetic signed
  assertions. Six negative controls reproduced unauthorized session creation
  in the old handler. The new product revision must be deployed to enforce it.
- The native integration suite passes 188 tests. Renderer defaults now follow
  the source lock; Headplane's build stage enables Node's proxy support for
  Corepack. All six native images built and the downloaded bundle passed
  checksum/source/image checks. Docker-host recovery preserved all 44 PostgreSQL
  tables and 16 runtime files, including signing material, Headscale keys,
  Headplane's database and the pending journal. Restored application health
  passed; all rehearsal containers are stopped.

The verified bundle is available at
`/mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.3-arm64-01` and on the server
under `/home/wjw/tailnet-rc3-arm64-20260914/`. Use its existing matching integration
checkout there. Backup took 32.0 seconds and the restore script 3.9 seconds;
application checks followed. An off-host copy of the synthetic backup is retained.

The three documented public hostnames currently have no DNS records. Tenant
grant/publication, confirmed DNS and public OIDC, reviewed ACLs and the physical
two-device pilot still need operator/device participation. Detailed execution
evidence is kept privately in `.runtime/local-wsl/audit/rc3-gate-execution.json`.
The assessments below describe earlier checkpoints, not production approval.

## Current assessment: RC3

Independently rechecked integration `507db66` (fix `cf8bf70`), with unchanged
product revisions. **The two RC2 code blockers are resolved; RC3 is ready for
the staging acceptance run, not production promotion.** No new blocker was
found in this focused review of revocation recovery and locking.

| Recheck | Result |
| --- | --- |
| Integration suite | 180 tests pass, including 45 new regressions; shell syntax and whitespace checks pass |
| Original partial-sync reproduction | Journal survives the second user's failed update; the next successful run expires Alice's nodes 11 and 12, writes the marker and clears the entry |
| Original failed-offboarding reproduction | Same successful recovery after a Headscale node-listing outage |
| Product source lock | All three checkouts match and are clean |
| RC3 bundle | Archive SHA-256, commit/lock/input binding, six local image IDs, platforms and revision labels verified with Podman |
| Promotion checker | Correctly rejects the candidate: `Release compatibility is not verified` |

Review covered journal-before-block ordering, Casdoor marker recovery, legacy
pending-state migration, full/staged retry, stale-marker clearing, and offboarding
and watch locks. Product builds, live tenant mutations and the Docker-host
restore were not rerun. Earlier results remain in the
[RC3 validation record](../releases/2026.09-rc.3-validation.md) and its RC2 references.

Use `/mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.3-01`, bound to
`507db66d1071a7ae2ecc6dfebffb330340e880ea`, for staging. Preserve that exact
integration checkout when importing; the historical RC2 bundle below is superseded.
Documentation edits made after this recheck do not change the existing bundle.

Remaining steps, in order:

1. Grant and publish `contact:user.employee:readonly` and
   `contact:user.phone:readonly`; optionally grant `tenant:tenant:readonly` for
   company verification. Run `--permissions` until `lifecycle_ready: true`.
   Review mappings before the scheduled worker's automatic transition to full
   lifecycle, then capture the first `mode: apply` report.
2. Deploy staging with real DNS/TLS, both OIDC clients, a reviewed ACL and two
   devices on different networks. Prove live node/key revocation for a test
   employee, denied reauthentication and Headplane session expiry.
3. Rehearse backup/restore on the Docker Compose v2 host. Record actual backup,
   migration and rollback references; retain the revocation journal with state.
4. Complete the manifest and promotion checks. The RC3 candidate's abbreviated
   image aliases and null commit/bundle/configuration fields are placeholders:
   copy full aliases and identities from the final bundle, and record successful
   evidence rather than only changing the compatibility flag.

Pending revocations are durable, but scheduled recovery still requires a run
to reach reconciliation after directory reads and Casdoor import/update checks.
If those dependencies keep failing during an incident, retry immediate
offboarding or use the [manual revocation procedure](operations.md); do not infer
device containment from a Casdoor block alone. On the first RC3 run, previously
worker-blocked RC2 accounts without a completion marker are revoked once again.

## Historical RC2 assessment

The remainder preserves the original RC2 findings and deployment sequence.
Its code blockers and test count describe `7cf3f64`, not RC3. Use the current
assessment above when deciding whether to proceed.

Reviewed 2026-09-14 at integration `7cf3f64`, Casdoor `fde2cf8f`, Headscale
`59516776`, and Headplane `fb7ae3e`. **RC2 is suitable for continued staging
validation; production promotion is blocked.** No deployment or branch was
promoted by this review.

## Verified and recorded evidence

| Check | Result |
| --- | --- |
| Source lock | All three product checkouts match and are clean |
| Local integration suite | 135 tests pass, plus shell syntax and whitespace checks |
| Offline bundle | Archive checksum, commit/lock/input binding, six local image IDs, platforms and revision labels verified with Podman |
| Running sandbox | Six containers running; worker reports healthy |
| Promotion checker | Rejects RC2: `Release compatibility is not verified` |
| Additional failure injection | Both revocation retry failures below reproduced using synthetic API fixtures |

This was a focused review of the latest integration and Casdoor hardening changes.
Go/Node product builds and their full suites were not rerun in this review;
their earlier results remain in the candidate validation records.

The [RC2 validation record](../releases/2026.09-rc.2-validation.md) and operator
handoff document the following earlier live results; this review did not repeat
the tenant mutations, authorization probe, or restore rehearsal:

- Staged worker every five minutes: 207 employees read, 207 Casdoor matches,
  zero unlinked accounts; employee numbers 207 (previously 206), departments 207,
  names 207, managers 201. Job title and mobile remain unavailable.
- Casdoor's partial-update authorization bypass was reproduced on the old image;
  all eight probe checks passed on the fixed, hardened image. Hardening protects
  directory fields and disables fallback identity binding and unwanted sign-in
  methods. Uploads persist; native sync logging and pagination are bounded.
- Live Headscale identity lookup and preauth-key expiry passed. No device was
  enrolled during that check, so live node expiry remains unverified.
- Isolated backup/restore recovered PostgreSQL, Headscale keys, and Casdoor
  signing material identically. The helpers require Docker Compose v2; the
  operator reports it is installed on the production host.

The existing bundle is
`/mnt/c/Users/wjw/Downloads/fysics-bundle-2026.09-rc.2-01`.
RC2/downstream branches exist locally in all four repositories; the handoff
reports that nothing was pushed. Private follow-up evidence is under
`.runtime/local-wsl/audit/`; keep its credentials and employee data private.

## Code blockers

1. **P1 — a partial sync can permanently lose a revocation.** In
   `sync/worker.py:887–911`, successful Casdoor blocks are collected in memory;
   retry state is saved only after every operation and the revocation attempt.
   Reproduction: freeze two fixture users, fail the second Casdoor update, then
   run successfully again. The first user stays blocked, the persisted pending
   list is empty, and both of that user's nodes remain active. The next plan
   does not emit `isForbidden: true` for an already-blocked account, so it never
   queues the lost revocation. A crash or ambiguous write response can cause
   the same gap. Persist recoverable revocation intent before applying the block,
   without prematurely advancing successful snapshot/missing-user state.
2. **P1 — immediate offboarding does not persist failed revocations.** In
   `sync/worker.py:942–947`, `--offboard --apply` blocks Casdoor, calls Headscale,
   and returns an error on failure without recording pending work. Reproduction:
   fail Headscale node listing during offboarding, restore it, then run the
   scheduled sync. The operator hold remains, the queue is empty, and both
   nodes stay active. Use durable retry state and serialize offboarding with the
   scheduled worker's state lock; the CLI currently returns before acquiring it.

These are independent of the tenant scopes. The passing suite tests a normal
Headscale failure during full sync and successful immediate offboarding, but
does not cover the two failure sequences above. Fix and add recovery regression
tests before building the next candidate. Until then, after any offboarding
failure explicitly verify/revoke the employee's nodes and preauth keys using
the [operations procedure](operations.md); do not rely on the next scheduled run.

## Remaining tenant and pilot gates

Run against the intended worker configuration:

```sh
python3 sync/worker.py --config /path/to/sync.json --permissions
```

The recorded tenant result is missing exactly `contact:user.employee:readonly`
(employment status/job title) and `contact:user.phone:readonly` (mobile). Use the
printed developer-console grant link, grant the scopes, and publish the app
version. Add `tenant:tenant:readonly` if enabling company-name verification.
Repeat the command until `lifecycle_ready: true` and check requested field coverage.

While employment status is unreadable, configured staged mode imports accounts
and available descriptive fields without assigning admission, roles, or new
blocks. Once every returned user has complete status, the next scheduled run
automatically enters full lifecycle mode; no configuration change is needed.
It can then change access. Review admission/role mappings before publishing the
grant, and capture the first `mode: apply` report and revocation counters. Pause
the worker first if a reviewed dry run is needed before that automatic transition.

On a staging site, validate real DNS/TLS and callbacks, both OIDC clients and
stable subjects, a reviewed ACL, and two devices on different networks. Offboard
a test employee with an enrolled device and prove live node/key revocation,
denied reauthentication, and Headplane session expiry. Recheck the Casdoor
authorization probe, rotate initial administrator credentials, and enable MFA.

## Deployment sequence after blockers are resolved

Use [local build and delivery](local-build-deploy.md) for detailed commands and
[the core guide](integrated-platform.md) or [Feishu recipe](deployment.md) for
application setup. RC2's existing bundle contains the bugs above; rebuild the
worker and export a new bundle from the corrected, committed candidate.

1. **Deliver the exact source and images.** From clean release checkouts run
   `bash scripts/test.sh` and `python3 scripts/release.py verify-sources ..`,
   build the pinned products/worker, and export a new six-image bundle. Transfer
   the whole bundle and ensure the server has the integration commit recorded
   in its manifest. Local-only branches cannot be fetched from GitHub until
   published; alternatively transfer the source with a Git bundle over SSH.
2. **Prepare the server.** Use Linux amd64, Docker Engine/Compose v2, Python
   3.11+, persistent storage, DNS and ports 80/443. Restrict SSH and keep database
   and internal service ports private. For a new site:

   ```sh
   cd /path/to/tailnet-stack
   umask 077
   mkdir -p .runtime
   cp deploy/site.example.json .runtime/site.json
   # Edit real hostnames, organization and client IDs before rendering.
   python3 scripts/configure.py --site .runtime/site.json --output .runtime
   python3 scripts/image-bundle.py import --bundle /path/to/new-bundle --runtime .runtime
   python3 scripts/image-bundle.py check --bundle /path/to/new-bundle --runtime .runtime
   stack() {
     docker compose --env-file .runtime/compose.env \
       -f deploy/compose.yaml -f deploy/compose.offline.yaml "$@"
   }
   stack --profile '*' config --quiet
   stack up -d db casdoor
   ```

3. **Bootstrap privately.** Follow the linked guide's temporary localhost
   origins and SSH tunnel procedure, replace the initial Casdoor password,
   configure signing material, OIDC applications/provider and hardening, and
   restrict access to the designated first Headplane owner. Restore public
   HTTPS origins before starting Caddy. Start Headscale, create/mount its API
   key, then enable Headplane. Configure and enable the optional worker only
   after reviewing identity and access mappings. Do not start all profiles
   blindly. Verify the intended owner before broadening access.
4. **Accept the staging deployment.** Complete the tenant/pilot gates above and
   the [acceptance checklist](acceptance-checklist.md). Back up and restore
   into an isolated project on Docker Compose v2; preserve the tested bundle,
   matching data backup, configuration and signing keys.
5. **Record and promote.** Copy `releases/manifest.example.yaml` to a private
   inventory and fill exact source/image/bundle identities, configuration
   checksums, migrations, backup/restore and rollback references. Validation
   entries must be mappings with `passed: true` and an `evidence` reference;
   RC2's narrative strings are not accepted. Follow
   [build/versioning](build-versioning.md) to record verified compatibility
   and bind the final lock, commit and bundle consistently. Run:

   ```sh
   python3 scripts/release.py check-promotion .runtime/production-manifest.yaml \
     --bundle-manifest /path/to/final-bundle/manifest.json
   ```

   A pass validates recorded fields; it does not execute the acceptance tests.
   Only then admit production users. For an upgrade, take a consistent
   pre-migration backup and retain prior images; rollback after migration
   requires restoring the matching database/configuration backup as well.
