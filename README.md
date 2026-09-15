# Tailnet Stack

A self-hosted private networking stack integrating Headscale, Headplane,
Casdoor, Caddy, and PostgreSQL, with deployment tooling and optional identity
and directory integrations. Feishu/Lark is the first implemented directory
adapter; the core stack can also use Casdoor-managed accounts or another
configured Casdoor login provider. Additional directory adapters require their
own implementation and identity/lifecycle validation.

The four source repositories stay separate. This repository owns the source
lock, Compose files, build scripts, runtime renderer, directory worker, backups,
and release evidence.

## Source and branches

The repository is [jwwang2003/tailnet-stack](https://github.com/jwwang2003/tailnet-stack).
`main` is the provider-neutral integration development branch:

```sh
git clone --branch main git@github.com:jwwang2003/tailnet-stack.git
cd tailnet-stack
```

Use `downstream/integrated-2026.09` for the current integration series and
`release/integrated-2026.09-rc.*` for pinned release candidates. An `-arm64`
suffix selects that candidate's ARM64 image configuration. Deploy the exact
integration commit matching the images or bundle, rather than a moving `main`.
Historical Feishu release refs remain available for existing deployments.

## External identity development

`main` implements [external Casdoor mode](docs/external-casdoor.md) alongside
bundled deployment. The implementation passes local regression, ownership and
artifact checks; live external-issuer login and deployment-host acceptance are
still pending. See the [development validation record](releases/external-casdoor-development-validation.md).
This is separate from the pinned RC4 candidate described below.

## Release status

`integrated-2026.09.0-rc.4` is a **release candidate, not production-ready**.
It combines the ARM64 candidate's fixes (Casdoor WebAuthn policy enforcement,
corrected sign-in hardening, renderer defaults from the source lock, Headplane's
Corepack proxy build fix) with restructured Headscale and Headplane downstream
patch series for cheaper upstream rebases; behaviour is unchanged. 188 integration
tests pass, the six-image bundle validates, and sandbox directory population and
restore rehearsals are recorded. Full Feishu lifecycle, public OIDC and the
two-device pilot remain incomplete. See the
[release review and deployment sequence](docs/production-readiness.md) for the
current source tuple, bundle and remaining gates.
The promotion checker must pass after the evidence has been reviewed:

```sh
bash scripts/test.sh
python3 scripts/release.py verify-sources ..
python3 scripts/release.py check-promotion .runtime/production-manifest.yaml \
  --bundle-manifest /path/to/bundle/manifest.json
```

The repository rename does not rebuild or promote RC4. Existing RC4 bundles
remain bound to their original integration commit and source-lock checksum;
validate them from that exact checkout. The new repository URL in `main`'s
lock is metadata for subsequent builds.

## Deploy

1. Read [integrated platform](docs/integrated-platform.md) for the architecture
   and the [Feishu recipe](docs/deployment.md) when using that integration.
   To use an independently managed identity service, follow
   [external Casdoor](docs/external-casdoor.md). Use its generated runtime Compose
   files; the repository-level Compose file is the legacy bundled deployment.
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

Use Python 3.11+ with [uv](https://docs.astral.sh/uv/). Initialize the local
environment and run the tools with:

```sh
uv sync --locked
uv run python scripts/huawei-swr.py --help
uv run --locked bash scripts/test.sh
```

`pyproject.toml` declares dependencies, `uv.lock` pins their versions, and
`.python-version` selects Python 3.12 for development. uv manages `.venv`.
The project is a collection of scripts, not an installable Python package;
the uv project version is tooling metadata, not the deployment release number.
`requirements-build.txt` is a generated compatibility export for pip-based
build instructions. After dependency updates, refresh it with
`uv export --locked --no-hashes --no-dev --no-emit-project -o requirements-build.txt`.
The development dependency group also retains Podman Compose for local sandbox
operations; it is not included in the pip build requirements export.

Keep credentials, employee
records, and runtime state outside Git. Store private release inventories separately;
commit only sanitized evidence. See [operations](docs/operations.md), the
[employee handbook](docs/employee-handbook.md), [languages](docs/languages.md),
and [upgrade notes](docs/branding-migration.md).
