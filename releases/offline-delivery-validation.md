# Offline image delivery validation

Status: implementation verified locally; live Windows build/SSH transfer/server load pending.

The preferred distribution path now builds four application images locally, includes the two pinned supporting images, and exports a six-image Docker archive. Import verifies content IDs, platform, source binding, and archive checksum before pinning runtime image references. The offline Compose overlay forbids registry pulls. This does not alter Feishu app permissions, secrets, database state, or application acceptance gates.

Validation performed:

- Full integration suite: 66 tests passed.
- Bundle tests use mocked Docker commands, including successful export/import/check, corrupt archives, wrong source/platform/revisions, failed export cleanup, and runtime backup/pin preservation.
- Build tests verify target-platform and worker revision labels as well as proxy behavior.
- Offline release-evidence checks validate local image identities without requiring registry digests; live compatibility and restore evidence remain mandatory.
- Documentation shell/Python/JSON snippets parse and local links resolve.
- Offline Compose overlay includes all six services, each with pull_policy=never.
- All three product checkouts still match versions.lock.yaml; no main/master changes.

No Docker Engine is available in the implementation environment, so no real images.tar has been built or loaded here. The user must run the commands in docs/local-build-deploy.md using Windows Docker Desktop/WSL and the remote deployment host. Tests do not claim network, registry, TLS, OAuth, or production readiness.
