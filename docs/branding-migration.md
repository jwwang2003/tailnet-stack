# Integrated Tailnet naming and migration

The platform integrates and extends Headscale, Headplane, and Casdoor. Feishu is
one optional login/directory adapter. Names for fresh builds and deployments are:

| Item | Current default | Legacy value |
| --- | --- | --- |
| Platform | Integrated Tailnet | Feishu identity integration |
| Release series | `integrated-2026.09` | `feishu-2026.09` |
| Release branch | `release/integrated-2026.09-rc.1` | `release/feishu-2026.09-rc.1` |
| Downstream/production branches | `downstream/integrated-2026.09`, `production/integrated-2026.09` | Corresponding `feishu-2026.09` names |
| Product version suffix | `-integrated.1` | `-feishu.1` |
| Local image namespace | `tailnet/` | `feishu/` |
| Offline content-ID aliases | `offline/tailnet-COMPONENT:sha256-HASH` | `offline/feishu-COMPONENT:sha256-HASH` |
| Compose project | `integrated-tailnet` | `feishu-tailnet` |

The GitHub remote and local repository directory remain
`tailscale-feishu-integration`. Upstream names/tags, published historical branches,
commits, release manifests, logs, and archived bundles keep their original names.
These local image tags are build outputs, not a promise of a public registry.

## Existing deployments

Keep your existing `.runtime/compose.env`. The renderer preserves every existing
`*_IMAGE` value and `COMPOSE_PROJECT_NAME`, including legacy tags, content-ID
aliases, registry digests, and names set during restore. It also preserves secrets
and network policy. It rewrites generated application settings, so back up local
manual configuration edits before rerendering. A fresh runtime uses neutral names.

Always pass that runtime's `compose.env` to Compose. Renaming the project of a live
installation can select a different PostgreSQL/Caddy volume and network, leaving
existing data attached to the old project. Branding alone needs no project rename,
volume migration, user migration, or credential rotation. Do not delete the old
runtime or recreate its volumes to obtain the new display name.

Existing image pins do not advance just because defaults change. Upgrade using a
reviewed release lock and newly built images/bundle, then deliberately update pins
through the documented import or registry workflow. Keep old images, manifests,
source refs, and a tested backup for rollback.

## Existing offline bundles

New exports use `offline/tailnet-*` aliases. Import/check and release validation
accept both neutral and legacy `offline/feishu-*` aliases, with exact component
and SHA-256 matching. Source-commit, lock/input checksum, platform, archive checksum,
and image revision checks remain mandatory.

A bundle is still bound to its original integration commit and lock/input files.
Use a checkout at the manifest's `integration_commit` to import/check a historical
bundle; do not edit its manifest or bypass source binding to make it look like the
new release. Old source refs are retained for that purpose. A runtime already
pinned to a legacy alias continues to use that image when rendered by the new code.

## Identity and directory data

Feishu-specific names such as the `feishu` worker configuration block, Casdoor
`lark` binding, `feishu-department-*` / `feishu-group-*` groups, `feishu_sync_*`
ownership markers, employee properties, native syncer IDs, and secret filenames
remain unchanged. They describe the adapter and durable identity state, not the
platform brand. Renaming them could orphan memberships or break stable linking.

Changing the actual identity provider is a separate identity migration: verify
subjects and account links, define admission/role ownership, and test revocation.
Disabling the worker stops reconciliation; it does not automatically remove its
existing users, groups, or permissions. Review and reassign that ownership before
switching directory sources.
