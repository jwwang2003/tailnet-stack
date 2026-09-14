# Integrated Tailnet

Deployment configuration for a self-hosted tailnet built from Headscale,
Headplane, Casdoor, Caddy, PostgreSQL, and an optional Feishu directory worker.
The four source repositories stay separate; this repository owns the lock file,
Compose files, build scripts, runtime renderer, worker, backups, and release
evidence.

## Release status

`integrated-2026.09.0-rc.3` is a **release candidate, not production-ready**.
The 168 local tests pass, the six-image bundle validates, and sandbox directory
population and isolated restore are recorded. RC3 fixes the two revocation retry
failures a follow-up review reproduced in RC2 (durable revocation journal and
Casdoor marker, regression-tested); full Feishu lifecycle, public OIDC, and a
two-device pilot remain incomplete. See the [release review and deployment sequence](docs/production-readiness.md).
The promotion checker must pass after the evidence has been reviewed:

```sh
bash scripts/test.sh
python3 scripts/release.py verify-sources ..
python3 scripts/release.py check-promotion .runtime/production-manifest.yaml \
  --bundle-manifest /path/to/bundle/manifest.json
```

## Deploy

1. Read [integrated platform](docs/integrated-platform.md) for the architecture
   and the [Feishu recipe](docs/deployment.md) when using that integration.
2. For an offline server, follow [local build and deploy](docs/local-build-deploy.md).
3. Copy `deploy/site.example.json` to `.runtime/site.json`, render it with
   `scripts/configure.py`, and keep `.runtime/` private.
4. Build or import the pinned images, validate the Compose file, then start the
   stack with the generated `.runtime/compose.env`.
5. Complete [acceptance](docs/acceptance-checklist.md), backup/restore, and the
   promotion checker before admitting production users.

Feishu is optional. If enabled, read [identity contract](docs/identity-contract.md),
[directory guide](docs/feishu-employee-directory.md), and [worker guide](sync/README.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `versions.lock.yaml` | Compatible source revisions and image names |
| `deploy/` | Compose, Caddy, and site template |
| `scripts/` | Build, configure, release, backup, and restore commands |
| `sync/` | Optional Feishu worker and contracts |
| `docs/` | Deployment, operations, identity, and user guidance |
| `releases/` | Immutable validation and promotion evidence |

Use Python 3.11+ with `requirements-build.txt`. Keep credentials, employee
records, and runtime state outside Git. Store private release inventories separately;
commit only sanitized evidence. See [operations](docs/operations.md), the
[employee handbook](docs/employee-handbook.md), [languages](docs/languages.md),
and [upgrade notes](docs/branding-migration.md).
