# Pilot and release acceptance

Record date, exact image digests/source lock, test environment, result, and evidence
for each item. Fixture tests validate implementation behavior; they do not replace
live Feishu permission, OAuth, or network tests. The development environment used
for this implementation lacked Docker and Go, so container builds, live tenant
OAuth, and a real database restore remain pending until exercised on a deployment
host. Do not set `compatibility_verified: true` while these checks are pending.

## Build and configuration

- [ ] All three service repositories are clean on downstream or production refs;
      fork-sync `main`/`master` branches were not modified.
- [ ] The lock contains the reviewed downstream Casdoor identity patch and exact
      commits for Headscale and Headplane; candidate images were built from those
      commits with recorded toolchains.
- [ ] Every deployed image, including worker, database, and proxy, is pinned by
      digest; the registry retains both candidate and previous release artifacts.
- [ ] Compose configuration and service-specific config validation succeed on the
      deployment host; public DNS/TLS and container file permissions are correct.
- [ ] Initial administrator credentials are replaced, Headplane owner bootstrap
      is restricted, employee registration is controlled, and API keys stay private.

## Feishu identity and directory lifecycle

- [ ] The Feishu App has the actual required permissions and access range; record
      the expected population and department/group scope.
- [ ] Login-before-sync and sync-before-login produce one stable Casdoor identity
      for the same Feishu app-scoped `open_id`, with no email-only account linking.
- [ ] Feishu login succeeds through Casdoor and OIDC discovery/signing-key
      validation succeeds for both Headscale and Headplane.
- [ ] Both clients receive the expected stable subject, verified email, groups,
      and reviewed Headplane role; two identities sharing an email cannot merge.
- [ ] Changed display name/email preserves identity; wrong tenant, missing groups,
      expired authorization code, bad OAuth state/nonce, and denied user are rejected.
- [ ] Nested departments, users in multiple departments/groups, rename/move,
      membership removal, disablement, deletion, and approved re-enablement converge.
- [ ] Pagination/rate-limit retries, timeout, interrupted run, permission loss,
      reduced scope, and malformed API responses do not remove unrelated access.
- [ ] A repeated complete synchronization is idempotent; removed users require the
      configured successful-snapshot confirmation before destructive lifecycle work.
- [ ] Broker outage and signing-key rotation behave as documented and recover.

## Tailnet and employee use

- [ ] An admitted employee registers a device on every supported operating system;
      a non-admitted employee cannot register or obtain new access.
- [ ] Repeated employee login links Headplane to the existing Headscale identity;
      the default role is `member`, with only reviewed administrators elevated.
- [ ] Default-deny ACL policy blocks traffic until an explicit tested policy allows
      approved services; admission claims alone do not grant network access.
- [ ] Device expiry/reauthentication, subnet routes, exit nodes if used, and DERP
      connectivity work from employees' actual networks.
- [ ] Offboarding blocks fresh login and revokes existing nodes, preauth keys,
      affected API keys, routes, and administrative access; test the 300-second
      Headplane session window and immediate containment procedure separately.
- [ ] An employee who did not build the system can follow the handbook to install,
      sign in, reach an approved service, and report a lost device.

## Operations and recovery

- [ ] Health/OAuth/sync/backup/TLS monitoring produces a useful alert for a forced
      failure and recovers without repeated unchanged notifications.
- [ ] A real backup succeeds with all writers stopped and restores the original
      running services; backup restart-failure handling is exercised.
- [ ] A real PostgreSQL/SQLite/key/session/sync/Caddy restore succeeds into a fresh
      isolated host/project using the explicitly matching backup source lock.
- [ ] Restored runtime paths and Compose project name remain correct after config
      rerender; new login, existing devices, and worker identity mappings are checked.
- [ ] A pre-upgrade backup restores successfully with the previous images after
      testing a candidate migration; downtime and recovery point are recorded.
- [ ] The release manifest references the completed reports, restore timestamp,
      lock checksum, image digests, and rollback artifacts; promotion checks pass.
