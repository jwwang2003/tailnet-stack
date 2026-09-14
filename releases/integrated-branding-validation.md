# Integrated Tailnet naming validation

The integrated release uses provider-neutral platform, project, image, version,
and branch defaults. Feishu remains the optional implemented directory adapter;
provider-specific identity fields and existing runtime pins retain their names.
See [migration notes](../docs/branding-migration.md) and the
[core deployment guide](../docs/integrated-platform.md).

Local validation on 2026-09-14:

- 120 integration tests passed across build, bundle, proxy, release/restore,
  runtime configuration, and worker suites. The full suite passed, followed by
  focused runtime/release checks after their final changes.
- New checks cover neutral configuration without Feishu credentials, the optional
  worker profile with no core service dependency on it, preservation of legacy
  runtime projects and all image pins, both offline alias namespaces, content-ID
  validation, and unchanged source-binding enforcement.
- Shell syntax and `git diff --check` passed.
- Docker Compose rendering was not run: the available Docker command delegates to
  Podman, and no Compose provider is installed in this environment. Compose YAML
  structure and profile boundaries were checked locally; CI retains its real
  Compose validation step.

The Headscale build helpers now target the downstream
`github.com/juanfont/headscale/hscontrol/types.VersionOverride` linker hook. The
coordinated product lock must include the commit introducing that hook before
building this release. Product binary/UI checks and final source-lock verification
belong to the coordinated release validation.

These checks do not claim live compatibility with every Casdoor provider, a new
Feishu tenant login, or a production restore rehearsal. The source lock remains
`compatibility_verified: false` until actual deployment evidence is recorded.
