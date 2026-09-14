# Headscale + Headplane + Casdoor/Feishu implementation plan

Historical planning baseline. Actual source pins and readiness are tracked in `../versions.lock.yaml`; deployment instructions and identity contracts supersede illustrative names/assumptions below. In particular, Casdoor group names cannot contain `/`; use stable ID names and qualified `organization/name` claims. The implementation uses an external worker plus narrowly scoped downstream product patches.

## Outcome

Deploy a maintainable tailnet for 20–200 employees with:

- Headscale as the control server;
- Headplane as the administration UI;
- Casdoor as the OIDC issuer;
- Feishu/Lark App OAuth as the login source;
- Feishu directory data synchronized into Casdoor;
- department/group membership available for access decisions;
- repeatable builds, upgrades, rollback, and operator/user documentation.

The integration must remain outside the Headscale and Headplane source trees wherever their supported OIDC and API interfaces are sufficient.

## Important discovery before implementation

Casdoor documents a Lark syncer that authenticates as an app, walks the department tree, and imports users. It requires at least `contact:user.base:readonly` and `contact:department.base:readonly` permissions. The current Casdoor source does **not** synchronize groups: its Lark syncer leaves group synchronization unimplemented. It also stores the synced Lark `user_id`, while the OAuth provider currently identifies a login with `open_id`. The first implementation spike must resolve that identity mismatch before any production data is imported.

References:

- [Casdoor Lark syncer](https://casdoor.ai/docs/syncer/Lark/)
- [Casdoor organization groups](https://casdoor.ai/docs/organization/organization-tree/)
- [Headplane SSO configuration](../../headplane/docs/features/sso.md)
- [Headscale OIDC configuration](../../headscale/docs/ref/oidc.md)

## Repository and ownership boundaries

Keep production integration files in a separate deployment area, for example:

```text
deploy/
  compose.yaml
  reverse-proxy/
  casdoor/
  headscale/
  headplane/
  sync/
  secrets/
  backups/
docs/
  operations.md
  employee-handbook.md
```

Do not place Feishu credentials, Casdoor exports, deployment manifests, or local patches inside `headscale/` or `headplane/`. Do not modify generated Headscale code or Headplane's OIDC implementation unless an acceptance test proves a supported configuration cannot meet the requirement.

## Subagent workflow

Each subagent works on a short-lived branch or worktree and must return:

1. a list of files changed;
2. commands run and their results;
3. unresolved assumptions;
4. an acceptance-test checklist;
5. a rollback note.

The coordinator merges only reviewed artifacts. Subagents must not edit another subagent's files or credentials.

### Workstream A — identity and Feishu discovery

**Agent:** `identity-research`

Tasks:

1. Verify the current Feishu App OAuth authorization-code flow, redirect URI rules, token endpoint, user-info endpoint, and required app permissions.
2. Verify whether the app can obtain organization-wide directory data or only the signing-in user's data.
3. Create a test Feishu app and record the minimum permissions needed for users, departments, and groups.
4. Verify whether `open_id`, `user_id`, or `union_id` is stable and returned consistently in login and directory APIs.
5. Produce a claim and identifier contract; no code changes yet.

**Gate:** Do not continue to production sync configuration until the identifier contract and tenant permission model are documented.

### Workstream B — Casdoor baseline and OIDC

**Agent:** `casdoor-baseline`

Tasks:

1. Pin a Casdoor release and database configuration.
2. Configure the Feishu/Lark OAuth provider using the app credentials.
3. Create one Casdoor organization and two applications: Headscale and Headplane.
4. Configure OIDC clients, redirect URIs, scopes, signing keys, issuer URL, and logout behavior.
5. Confirm that Casdoor emits a stable `sub`, `email`, `name`, and group claim.
6. Test login before and after a Casdoor user is created by the Lark syncer.

**Gate:** A test user must complete Feishu login and obtain a valid OIDC token accepted by a standalone OIDC test client.

### Workstream C — directory synchronization

**Agent:** `directory-sync`

Tasks:

1. First test the native Casdoor Lark syncer; do not duplicate it prematurely.
2. Determine exactly which user fields, departments, memberships, and disabled states it imports.
3. Implement only missing functionality in a separate sync worker. The likely missing area is Feishu user-group synchronization.
4. Use Feishu IDs as keys and maintain an explicit mapping table.
5. Represent departments and Feishu groups with namespaced Casdoor groups, such as:

   ```text
   feishu/departments/engineering
   feishu/departments/engineering/platform
   feishu/groups/tailscale-users
   ```

6. Make each sync run idempotent, paginated, rate-limit aware, and safe to retry.
7. Implement deletion/disablement as a two-step process: mark stale first, disable only after a successful complete sync. A failed partial sync must never remove access.
8. Emit a sync report containing counts, skipped records, permission errors, and the source watermark.

**Gate:** A fixture containing nested departments, multiple memberships, renamed departments, a deleted user, and a disabled user must converge to the expected Casdoor state after repeated runs.

### Workstream D — Headscale and Headplane integration

**Agent:** `tailnet-integration`

Tasks:

1. Configure Headscale to use Casdoor discovery, authorization-code flow, PKCE, and the required `groups` scope.
2. Configure `allowed_groups` for the employee population.
3. Configure Headplane with the same issuer and a separate OIDC client.
4. Keep Headplane's default role as `member`; grant administrative roles through an explicit Casdoor role claim only for a small, reviewed group.
5. Confirm Headplane's subject linking works with Casdoor's `sub` and Headscale's provider identifier.
6. Test first login, repeated login, client-specific subject stability, and email changes.
7. Document the distinction between Headscale OIDC admission groups and Headscale ACL policy groups. Headscale's OIDC group claim is an admission filter; it is not automatically a dynamic ACL policy source.

**Gate:** A new employee can register a node and sign into Headplane; a user outside the allowed group cannot; removing membership prevents new authentication after sync.

### Workstream E — deployment, build, and release engineering

**Agent:** `release-operator`

Tasks:

1. Create a production Compose or systemd deployment outside the developer-only `headplane/compose.yaml`.
2. Pin image digests or release tags for Casdoor, Headscale, Headplane, reverse proxy, and sync worker.
3. Pin the tested Headscale/Headplane compatibility tuple. The current checkouts are untagged SHAs, so they are not a production release baseline.
4. Store secrets in mounted files or a secret manager; never commit them.
5. Configure persistent storage, SQLite WAL, scheduled backups, certificate renewal, health checks, metrics, and structured logs.
6. Add a release manifest recording versions, image digests, configuration checksums, and migration status.
7. Write upgrade and rollback scripts. Headscale migrations must be treated as one-way: a rollback requires restoring a pre-migration database backup before starting the old image.
8. Validate restore on a clean host before production cutover.

**Gate:** A clean host can be built from the documented release manifest, and the previous release can be restored from backup without manual database editing.

### Workstream F — documentation and handbook

**Agent:** `documentation`

Produce two documents:

#### Operator guide

Cover:

- prerequisites and DNS/TLS;
- Feishu app creation and permission approval;
- Casdoor initialization and OIDC clients;
- syncer configuration and permission scope;
- Headscale configuration and API key handling;
- Headplane configuration;
- ACL and group policy examples;
- backup and restore;
- logs, metrics, health checks, and alert thresholds;
- user offboarding and emergency access revocation;
- version pinning, upgrade, rollback, and migration rules;
- disaster recovery exercise.

#### Employee handbook

Cover:

- installing the Tailscale client;
- signing in with Feishu;
- registering a device;
- naming devices;
- approving or reauthenticating a device;
- subnet routers and exit nodes;
- accessing approved internal services;
- Headplane access for authorized administrators;
- lost-device reporting;
- leaving the organization;
- support and troubleshooting steps.

Do not expose API keys, app secrets, recovery codes, or administrator procedures in the employee handbook.

## Required test matrix

### Authentication

- login, callback state mismatch, nonce mismatch, expired authorization code;
- revoked Feishu user;
- changed email and display name;
- duplicate email with different Feishu identity;
- Casdoor signing-key rotation;
- Casdoor outage and recovery.

### Synchronization

- initial full import;
- nested departments;
- user in multiple departments;
- Feishu group membership changes;
- rename and move department;
- disable, delete, and re-enable user;
- pagination and rate limiting;
- interrupted run and safe retry;
- partial permission failure;
- identity mismatch between `user_id` and `open_id`.

### Tailnet lifecycle

- first Headscale OIDC registration;
- Headplane first-user owner bootstrap;
- repeated login with both clients;
- node expiry and reauthentication;
- offboarding a user with existing nodes;
- revoking Headplane sessions;
- restore from backup;
- upgrade and rollback.

## Definition of done

The implementation is ready for a pilot when:

1. A documented Feishu App can authenticate a user through Casdoor.
2. Casdoor issues OIDC tokens accepted by both Headscale and Headplane.
3. The identifier contract has passed login-before-sync and sync-before-login tests.
4. Departments, groups, memberships, and disablement converge correctly.
5. A removed employee cannot obtain new access, and the operator procedure revokes existing node/session access.
6. A pinned release can be rebuilt on a clean host.
7. Backup restore and rollback have been exercised.
8. Operator and employee handbooks are complete and reviewed by someone who did not implement the system.

## Suggested delivery order

1. Identity-research spike and Feishu permission approval.
2. Casdoor baseline and OIDC test client.
3. Identifier compatibility spike.
4. Native syncer evaluation and group-sync worker.
5. Headscale/Headplane integration.
6. End-to-end lifecycle tests.
7. Production deployment and restore rehearsal.
8. Operator guide and employee handbook.
9. Ten-user pilot, then staged rollout to the full team.
