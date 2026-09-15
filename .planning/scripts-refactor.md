# Scripts and CLI refactoring proposal

Status: proposed, not implemented. Baseline: `4d1b33f9f58957b8a56966526c926d43ad10065b`
plus the existing uncommitted documentation consolidation. This proposal is recorded
separately from that documentation work. It is maintainer material, outside user navigation;
archive it after implementation and document only final commands in the Stack guide.

## Objective and findings

Provide one discoverable `uv run tailnet …` command, importable application logic,
and a small set of clearly scoped development helpers.

The current `scripts/` contains 17 files and approximately 2,800 lines. Its main
problems are mixed responsibilities and coupling, rather than file count:

- CLI parsing, validation, API calls, subprocesses, and presentation share files.
- Most commands use argparse; SWR already uses Typer/Rich, both locked dependencies.
- Python scripts import neighboring scripts through `sys.path` edits; tests and CI
  dynamically load hyphenated filenames and patch module globals.
- Several modules derive the checkout from their own path. A move to `src/` would
  break template lookup and source/bundle binding without an explicit project context.
- Bash recovery exchanges positional text with `recovery.py`; build helpers also
  parse release CLI output. Changing formatting can break orchestration.
- Recovery ownership, source/image binding, credential handling, and schema-1/2
  compatibility already have regression coverage and must survive the refactor.

Use the existing Typer and Rich dependencies. uv manages the environment and
entry point; it is not the CLI framework. No additional CLI framework is needed.

## Target layout

```text
src/tailnet_stack/
  __init__.py
  __main__.py
  cli/                    # Typer groups, argument parsing, output and exit mapping
    app.py
    configure.py
    images.py
    release.py
    recovery.py
    identity.py
    doctor.py
  context.py              # Explicit project root, runtime, lock and deployment selection
  deployment.py           # Existing ownership/selection contract
  configuration.py        # Rendering and private file handling
  images/                 # Build orchestration, bundle validation and SWR publication
    build.py
    bundle.py
    swr.py
  release.py              # Source verification and promotion evidence validation
  recovery.py             # Backup/restore validation and orchestration
  identity/               # Administrative Casdoor client, hardening and synthetic probes
    casdoor.py
    hardening.py
    probes.py
  support/                # Shared mechanisms with real callers
    process.py
    files.py
    proxy.py
scripts/
  dev/                    # Test runner, native smoke check, interactive SSH proxy helper
  …                       # Temporary forwarding wrappers at existing paths
tests/
  cli/                    # New command and compatibility contracts
  …                       # Existing suites, progressively using normal imports
deploy/                   # Existing templates; one source of truth
build/                    # Existing image recipes
sync/                     # Separate lightweight directory worker and configuration
```

Keep CLI modules thin and dependency direction one-way: commands call domain
functions; domain functions never import Typer/Rich. Domain functions return data
or raise meaningful errors. Share file/process mechanisms without merging
image-archive and backup-archive validation policies, which intentionally differ.
Split modules further only when distinct responsibilities justify it.

## Command surface

All commands below are proposed. Existing guide commands remain valid until migrated.

| Current entry point | Proposed command |
| --- | --- |
| `configure.py` | `tailnet configure --site SITE --output RUNTIME` |
| `build-products.sh` | `tailnet --runtime RUNTIME images build --workspace WORKSPACE` |
| `build-headscale.sh` | `tailnet --runtime RUNTIME images build-headscale --workspace WORKSPACE` |
| `image-bundle.py` | `tailnet --runtime RUNTIME images bundle export/import/check …` |
| `huawei-swr.py` | `tailnet --runtime RUNTIME images push --registry swr …` |
| `release.py` | `tailnet --runtime RUNTIME release verify-sources/artifacts/check-promotion …` |
| `backup.sh`, `restore.sh` | `tailnet --runtime RUNTIME backup --output ARCHIVE`; `tailnet restore ARCHIVE --expected-lock LOCK --output DEST --project NAME --expected-mode MODE` |
| `casdoor-harden.py`, `verify-casdoor-authz.py` | `tailnet --runtime RUNTIME identity harden/probe …` |
| `sync-mirror-no-proxy.py` | `tailnet doctor proxy` (preview), with explicit `--apply` for configuration writes |
| `prepare-podman-buildfile.py`, `recovery.py` internals | Internal functions; no separate public command |
| `smoke-headscale.py`, `remote-proxy-shell.sh`, `test.sh` | Development helpers; move under `scripts/dev/` after callers migrate |

Retain source-field/support-image/platform queries as documented release commands
where external callers need them; internal build code should call Python functions.
Preserve SWR's explicit `--image` override, environment options, and offline dry run.

Common command rules:

- Root options precede subcommands. `--project-root` selects the integration checkout;
  otherwise discover it from the working directory using project markers, never
  the installed module path. Runtime-sensitive commands require a verified runtime;
  `configure` and `restore` create destinations and do not require an existing one.
- Derive deployment metadata from `--runtime`. Allow explicit `--deployment` for
  artifact-only use; when both are supplied, verify agreement before side effects.
  Preserve bundled fallback only in legacy wrappers, not as silent recovery from
  malformed/missing explicitly selected metadata.
- Keep help, version, and offline previews free of Docker/API calls. Importing a
  module must not read credentials or mutate files.
- Use Rich for readable terminal output and a plain, documented `--json` result
  for automation. Keep diagnostics/progress on stderr; preserve legacy wrappers'
  stdout and exit contracts while callers depend on them.
- Preserve existing preview/apply behavior per operation; do not add blanket
  prompts or pretend a mutating command supports a harmless dry run. Probes use
  synthetic resources and must remain explicitly described as mutating.
- Centralize error presentation and subprocess invocation with argument arrays,
  redacted diagnostics, password stdin, and restricted credential environments.
  Disable local-variable dumps in CLI exceptions. Propagate interruption status.

## Packaging and resource boundary

Replace `[tool.uv] package = false` with an installable `src` package, an explicitly
configured build backend (prefer `uv_build`), and a console entry point:

```toml
[project.scripts]
tailnet = "tailnet_stack.cli.app:main"
```

Resolve and lock the build backend during implementation. Keep Python >=3.11,
existing locked CLI dependencies, and the project's tooling version distinct from
deployment release versions. Refresh the pip compatibility export when needed.

The CLI operates on an explicit integration checkout. Keep templates, locks, and
build recipes there; do not copy them into a package and create competing versions.
An installed wheel must still accept `--project-root`; source-bound operations
must verify the selected checkout matches the executing tooling revision. Define
and test that provenance binding before enabling wheel-based release operations.
Neither an editable install nor a module relocation may bypass clean-checkout or
exact-commit validation. Historical bundles remain usable from their original checkout.

Keep `sync/` and its standard-library-only Docker entry point intact in this
refactor. Do not install the administrative CLI into the worker image. Any later
worker decomposition must preserve state/journal formats, locks, scheduling,
offboarding, and container entry-point behavior in its own change.

## Delivery sequence

| Step | Work | Exit condition |
| --- | --- | --- |
| 1. Record contracts | Run the existing suite; capture command arguments, output/exit behavior, env handling, generated files, source bindings, and mocked external call traces. Inventory shell, CI, tests, Dockerfile and guide callers. | Repeatable baseline; unknown external script callers identified. |
| 2. Establish package/context | Add packaging, `tailnet --help`, project/runtime resolution, and typed errors. Move deployment selection intact. | Fresh `uv sync --locked`, imports, help, editable/wheel installation and explicit-root checks pass without infrastructure access. |
| 3. Extract domain code | Move configuration, source/release checks, bundles, SWR, and identity tools one domain at a time. Replace dynamic imports in its tests. | Existing behavior tests pass through package imports and old commands; generated files and rejection boundaries match. |
| 4. Connect CLI groups | Add consistent Typer groups and structured output over extracted functions. Keep old scripts as thin adapters, not duplicate implementations. | New/legacy parity for argument handling, output, errors, defaults, previews, and credential redaction. |
| 5. Refactor orchestration | Port build and recovery orchestration only after extracting stable domain functions; initially the CLI may invoke unchanged shell helpers. Keep the interactive SSH tunnel as a focused shell helper. | Backup signal/failure cleanup, original-service restart, locks, atomic no-overwrite publication, restore isolation, and Docker/Podman build traces remain correct. |
| 6. Switch callers and simplify | Update CI, build callers, dev helpers and Stack guide. Document wrappers for one compatibility release, then remove them after caller coverage proves migration. | Normal workflows use `tailnet`; only intentional dev helpers and explicitly temporary adapters remain in `scripts/`. |

Do not combine the refactor with deployment schema changes, dependency upgrades,
new registries/providers, identity migration, or live production actions. Preserve
the uncommitted documentation work throughout. The unfinished workspace-level
`huawei-swr.py` is outside this repository; do not delete or import it as part of cleanup.

## Validation and completion

- All existing regression suites pass. New tests target command contracts and
  packaging/context changes; avoid tests that merely mirror the new module layout.
- Bundled, external, and external-local-sync outputs and selected artifacts agree
  across configure/build/bundle/SWR/backup/restore/promotion operations.
- Schema-1 archives and schema-2 bindings still reject corrupt, extra, substituted,
  unsafe, or mismatched content before side effects.
- Recovery interruption and partial-failure tests prove only originally running
  owned services restart, including failure to restart; external identity remains
  untouched and restored application writers stay stopped.
- Tests and CI use normal imports. No production `sys.path` mutation, dynamic
  loading of sibling scripts, or shell parsing of human-formatted CLI output remains.
- Native Docker Compose and synthetic recovery rehearsal pass after orchestration
  changes; Docker/Podman build and image-delivery behavior is exercised on the
  intended architecture. Report live checks separately from mocked command traces.
- Compare restored persistent state and generated configuration with the baseline;
  update only active guide commands and references. Preserve archived documentation,
  historical manifests and release artifacts.

Rollback during development means restoring the previous tooling code and entry
points; this refactor must not require data conversion. Published release revisions
and their original tools remain available for existing bundles and backups.

## Implementation references

- [uv project entry points and packaging](https://docs.astral.sh/uv/concepts/projects/config/#entry-points)
- [Typer command groups](https://typer.tiangolo.com/tutorial/subcommands/)
- Local dependency selection: `pyproject.toml` and `uv.lock` (Typer 0.27.2, Rich 15.0.0 at inspection).
