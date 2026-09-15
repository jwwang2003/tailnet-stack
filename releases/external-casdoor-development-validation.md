# External Casdoor development validation

Date: 2026-09-15. Implementation source: `20cec5a522d96abdaedf6a5d83eaedf700ac16bd`.
Status: **development implementation, ready for isolated staging acceptance;
not a production promotion or live migration**. Existing RC4 pins/manifests are
unchanged. This record concerns deployment tooling; the product binaries remain
at the source revisions recorded in the lock.

## Delivered

- Explicit bundled/external identity mode, exact HTTPS issuer, and local/external/
  disabled directory ownership; legacy inputs retain bundled behavior.
- External rendering imports separately registered client secrets, requires
  distinct client IDs, and preserves credentials on rerender. Identity mode,
  issuer, directory ownership and existing external client IDs cannot silently
  change. No external identity provisioning is performed.
- Runtime-generated Compose, offline overlay and proxy files bind to a deployment
  descriptor. External mode omits Casdoor/PostgreSQL/resources and, by default,
  the worker. No profile can restore an omitted service.
- Source verification, builds, default SWR uploads, offline bundles and promotion
  checks share deployment selection. New bundles use schema 2; historical schema 1
  archives retain their bundled rules.
- External backup/restore excludes identity database/state and unowned worker
  credentials, validates issuer/mount ownership, and leaves restored writers
  stopped. Legacy issuer information is preserved during backup conversion.
- Scoped hardening and probes detect adjacent runtime descriptors, reject
  contradictory selections, and require explicit external application/fixture
  boundaries. Organization mutation is separately selected.

## Agent execution and review

Three isolated implementation worktrees covered configuration, recovery, and
artifact/release tooling. The coordinator owned shared contracts, identity
boundaries, integration, documentation and acceptance checks. Agents then reviewed
one another's work; fixes were rechecked independently.

Review closed client ID/secret mismatches, omitted-descriptor hardening bypass,
shared named-volume overrides, inconsistent runtime issuers, legacy rerender
failure, empty runtime bindings, legacy import fallback for missing descriptors,
and undeclared images in schema-2 archives. Actual Podman export exposed legitimate
shared-layer links; the validator now accepts only safe internal layer links and
still rejects escaping, cyclic, metadata, and directory links.

## Checks performed

| Check | Result |
| --- | --- |
| Full `uv run --locked bash scripts/test.sh` | 265 tests pass; shell syntax and diff checks pass |
| Renderer/descriptor/client/hardening regressions | Pass, including no mutation before validation errors |
| Recovery command traces | Pass for owned stop/restart, no external database calls, no application/worker startup on restore, and failure cleanup |
| Podman Compose 1.6.0 resolved main + offline definitions | Bundled: 6 services; external: 3; external + local sync: 4 |
| Caddy 2.10.2 parser in disposable network-disabled containers | All three generated proxy configurations parse |
| Real filesystem external backup staging, archive validation and extraction | Synthetic Headscale/Headplane state preserved; no identity database/state included; restored runtime passes preflight |
| Selected source verification and default SWR preview | External selects Headscale, Headplane and reverse proxy only |
| Actual Podman schema-2 export and subsequent bundle validation | Exactly 3 selected amd64 images; source/configuration/archive/inventory checks pass |
| Legacy bundle and backup regression tests | Pass |

Compose static checks locally used explicit `apps`, `public`, and `sync` profiles:
Podman Compose 1.6.0 does not expand literal `--profile '*'`. CI now includes a
native Docker Compose matrix with the wildcard and both generated Compose files.
Local static checks do not substitute for that native check or actual Docker-host
service/recovery acceptance.

The isolated test bundle is `/tmp/tailnet-external-accept-bundle`, bound to the
implementation commit above, with archive SHA-256
`6e30cb94ccedbff21ca509e9ff69fe0818b770b2b8362962ee077d2e04b9cea1`.
It contains Headscale, Headplane and Caddy, uses synthetic example-domain deployment
metadata, and is a development test artifact. It is not a production inventory.
Use its original source checkout to validate its commit binding; changing the
source tree does not change the archived integration commit.

## Outstanding acceptance

- An independently managed HTTPS test Casdoor and both complete OIDC login flows,
  including stable subjects, groups, roles, rejected identities and issuer outage.
- Real Tailnet stop/teardown while a second OIDC client remains available, proving
  shared identity data and service availability on the intended host.
- Docker-host backup/restore with real running applications and database state,
  plus external-local-worker lifecycle/revocation against synthetic identities.
- ARM64 artifact execution/delivery and registry push/pull on the target platform.
- Reviewed external issuer version/recovery evidence and completed promotion
  inventory. `compatibility_verified` remains false.

No live Casdoor or employee data was migrated, no existing services restarted,
and no SWR images were published. Public DNS/TLS acceptance, physical-device
checks, and Sub2API session/API-key revocation remain outside these results.

See [external deployment instructions](../docs/external-casdoor.md) and the
[agent implementation plan](../docs/external-casdoor-plan.md).
