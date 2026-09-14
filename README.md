# Integrated Tailnet

An integrated, extended deployment of Headscale, Headplane, and Casdoor for a
20–200 person tailnet. Headscale controls the network, Headplane provides its
administration UI, and Casdoor provides identity and OIDC for both services.
This repository owns deployment configuration, optional directory adapters,
source locks, build and offline delivery tools, and operating instructions.

```text
Selected identity provider / Casdoor accounts → Casdoor OIDC → Headscale + Headplane
Optional Feishu Contact API → Feishu sync worker → Casdoor users and groups
```

Feishu is an optional login and employee-directory integration. Core configuration
requires no Feishu app, credentials, or API access; the worker runs only when
explicitly selected. Other providers use Casdoor's configuration and must meet the
same stable-subject, admission-group, and role contract. The supplied directory
worker implements Feishu only; it is not a generic directory connector.

## Getting started

1. Read the [core deployment guide](docs/integrated-platform.md) and
   [build/versioning workflow](docs/build-versioning.md).
2. [Build locally, transfer over SSH, and load on the server](docs/local-build-deploy.md).
   No registry subscription is required. The six-image bundle includes PostgreSQL,
   Caddy, and the optional worker. [Rootless Podman in WSL](docs/podman-build.md)
   is supported for local builds and export; the server uses Docker.
3. Configure Casdoor and your selected identity source, then validate OIDC login,
   device enrollment, admission, and Headplane ownership.
4. If using Feishu, follow the [Feishu setup recipe](docs/deployment.md),
   [identity contract](docs/identity-contract.md), and [worker guide](sync/README.md).
   The [employee-directory guide](docs/feishu-employee-directory.md) covers 部门、职务、工号,
   phone and email. Directory reads require approved app permissions and scope.
5. Complete [acceptance](docs/acceptance-checklist.md) and a backup/restore rehearsal
   before production promotion. Customize the [employee handbook](docs/employee-handbook.md)
   for the provider, URLs, and client versions you actually deploy.

[Operations](docs/operations.md) covers backup/restore, incidents, and upgrades.
For existing installations, read the [branding migration notes](docs/branding-migration.md)
before rerendering configuration or changing releases.

## Repository boundaries

- `headscale`, `headplane`, and `casdoor` remain separate forks based on official
  releases. Each keeps upstream names and focused downstream extensions.
- [versions.lock.yaml](versions.lock.yaml) records compatible source revisions and
  product image names; [image-inputs.json](image-inputs.json) records supporting images.
- `deploy/compose.yaml` and `deploy/Caddyfile` define the stack;
  `scripts/configure.py` renders private settings into ignored `.runtime/`.
- The optional Feishu adapter lives in `sync/`, build recipes in `build/`, and
  immutable release evidence in `releases/`.
- The existing GitHub repository name `tailscale-feishu-integration` remains its
  checkout/remote location. It does not limit the platform's identity providers.

Local checks:

```sh
python3 -m pip install -r requirements-build.txt
bash scripts/test.sh
python3 scripts/release.py verify-sources ..
```

The lock is a release candidate, not production acceptance evidence. Promotion
requires live selected-provider login, stable identity linking, OIDC acceptance,
account lifecycle and access revocation, and backup/restore verification. Local
fixture tests do not establish compatibility with an untested provider or tenant.

For switchable English and Chinese UI, see [language settings](docs/languages.md).
