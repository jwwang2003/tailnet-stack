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

## Layout

| Path | Purpose |
| --- | --- |
| `deploy/headscale/` | Headscale configuration and network policy templates |
| `deploy/headplane/` | Headplane configuration templates |
| `deploy/casdoor/` | Casdoor bootstrap/configuration templates |
| `deploy/reverse-proxy/` | HTTPS routing configuration |
| `deploy/sync/` | Synchronization runtime configuration |
| `sync/` | Directory reconciliation worker |
| `docs/` | Build/release, operator, and employee instructions |
| `releases/` | Non-secret release manifest examples and release records |

Read [build and versioning](docs/build-versioning.md) before changing source refs,
and [deployment boundaries](deploy/README.md) before preparing a host. Runtime
secrets, databases, and backup files are excluded from Git.

The lock starts as a development baseline. Release promotion requires real Feishu
OAuth, directory permissions and identity linking, downstream OIDC acceptance,
offboarding, and backup/restore verification. Source tags alone are not proof of a
working deployment.
