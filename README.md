# Feishu identity integration for Headscale

This repository owns the deployment, Feishu directory synchronization, release
locks, and operating instructions for a 20–200 person tailnet. Headscale controls
the network, Headplane provides its administration UI, and Casdoor brokers Feishu
App OAuth into OIDC for both services.

```text
Feishu App OAuth ──> Casdoor OIDC ──> Headscale + Headplane
Feishu Contact API ──> sync worker ──> Casdoor users and groups
```

Feishu app credentials and the app's approved directory scope are required for a
real tenant deployment. A successful OAuth login alone does not authorize access
to the entire employee directory. Department or group claims used to admit users
to Headscale do not automatically become Headscale network ACL groups.

## Repository boundaries

- `headscale`, `headplane`, and `casdoor` remain separate forks based on official
  release tags. Avoid downstream changes where supported APIs and configuration
  meet the requirement.
- This integration repository records the compatible source revisions together
  in [versions.lock.yaml](versions.lock.yaml).
- A Casdoor patch, if required by verified OAuth/sync behavior, belongs in the
  Casdoor fork with a focused test; deploy the patched commit recorded in the lock.
- Feishu directory synchronization and deployment files live here, so upstream
  updates do not overwrite them.

## Getting started

**Recommended: [build locally on Windows, transfer over SSH, and load on the server](docs/local-build-deploy.md).** No registry subscription is required; the six-image bundle includes PostgreSQL and Caddy.

1. Read [build and versioning](docs/build-versioning.md) and verify `versions.lock.yaml`.
2. Follow the [deployment runbook](docs/deployment.md) to render private configuration and bootstrap Casdoor.
3. Configure the [Feishu worker](sync/README.md) and validate the [identity contract](docs/identity-contract.md).
4. Run the [acceptance checklist](docs/acceptance-checklist.md) before production promotion.
5. Give employees the [handbook](docs/employee-handbook.md), with your real URLs and tested client versions.

[Operations](docs/operations.md) covers backup/restore, offboarding, incidents, and upgrades.
`deploy/compose.yaml` and `deploy/Caddyfile` provide the stack; `scripts/configure.py`
renders secrets and per-service settings into ignored `.runtime/`. The worker is
in `sync/`; build recipes are in `build/`; immutable release evidence belongs in `releases/`.

Local checks:

```sh
python3 -m pip install -r requirements-build.txt
bash scripts/test.sh
python3 scripts/release.py verify-sources ..
```

The lock starts as a development baseline. Release promotion requires real Feishu
OAuth, directory permissions and identity linking, downstream OIDC acceptance,
offboarding, and backup/restore verification. Source tags alone are not proof of a
working deployment.
