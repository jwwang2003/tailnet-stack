# Feishu directory worker

For 部门、职务、工号 and optional phone/email extraction, see [employee-directory setup](../docs/feishu-employee-directory.md).

The worker adds department/group memberships, offboarding, and existing-access revocation to the corrected native Casdoor Lark importer. It uses Python 3.11+ and the standard library. Run it as one replica against a persistent state directory.

```sh
python3 sync/worker.py --config /path/to/sync.json --permissions
python3 sync/worker.py --config /path/to/sync.json --audit
python3 sync/worker.py --config /path/to/sync.json
python3 sync/worker.py --config /path/to/sync.json --apply
python3 sync/worker.py --config /path/to/sync.json --apply --watch
python3 sync/worker.py --config /path/to/sync.json --profile-only --apply
python3 sync/worker.py --config /path/to/sync.json --offboard employees/NAME --apply
```

`--permissions` reports granted and missing Feishu app scopes without reading the directory. `--audit` reads the complete permitted directory and reports counts only. The bare command is a read-only dry run of a synchronization. `--apply` also runs the native profile importer, applies the reviewed reconciliation, revokes the Headscale access of newly blocked users when `headscale` is configured, and atomically writes state. `--watch` repeats at `interval_seconds`; a failed interval logs an error and retries at the next interval. One-shot failures exit nonzero. `--profile-only` writes descriptive employee properties only, and `--offboard` blocks one account immediately; both are dry runs without `--apply`. Every success emits a JSON report with `mode` (`dry-run`, `apply`, `dry-run-staged`, `apply-staged`, `profile-only-dry-run`, `profile-only-apply`, `audit`, `permissions`, `offboard-dry-run`, or `offboard-apply`), source counts, unmatched users, pending missing users, blocks/re-enables, operation count, revocation counters, completion timestamp, and scope hash. Reports contain no tokens, user profiles, or secrets.

Start from `config.example.json`. Mount credentials as files and keep the populated config private. `feishu.base_url` selects `https://open.feishu.cn` or `https://open.larksuite.com`; the native syncer's `host` and `user` must match that origin and app ID. The configured tenant key binds durable state to the operator's intended tenant; it is not API-attested tenant verification. The enterprise app's ownership and authorized data range are the actual directory trust boundary.

`casdoor.client_id`/secret must belong to a dedicated server-side application able to read the selected organization's users/groups and syncer, invoke that syncer, create/update groups, and update user groups/properties/status. In the pinned Casdoor release, these app credentials carry broad management API authority; selecting an organization does not scope the credential. The worker enforces its own organization/field boundaries. Basic authentication keeps the secret out of URLs. Keep plain HTTP Casdoor calls on the private container network; use HTTPS for remote hosts. The worker refuses HTTP redirects, bounds retries and response sizes, and does not retry uncertain writes automatically.

Configure Casdoor's native Lark syncer with `isEnabled: false`, `isReadOnly: true` (no attempted writes back to Feishu), the matching organization and Feishu app, and profile-only `tableColumns`. Example:

```json
[
  {"name": "Lark", "casdoorName": "Lark", "isKey": true, "isHashed": false},
  {"name": "DisplayName", "casdoorName": "DisplayName", "isHashed": true},
  {"name": "Email", "casdoorName": "Email", "isHashed": true},
  {"name": "Avatar", "casdoorName": "Avatar", "isHashed": true},
  {"name": "Title", "casdoorName": "Title", "isHashed": true}
]
```

Use exactly one `isKey: true` column: `Lark`, carrying the corrected `open_id` binding. Display names and emails cannot be keys. Keep `Name`, `Id`, `Groups`, `Properties`, and `IsForbidden` out of these columns. Matching Casdoor-cased names are an integration convention, not an upstream hashing requirement. The worker invokes the corrected native syncer serially only after the Feishu snapshot and existing Casdoor state pass validation. This still is not a cross-service transaction: a later API failure can leave an applied prefix of changes. The next successful run reconciles it; the missing-user counters advance only after every operation succeeds. Do not run Casdoor's native schedule or another worker against the same organization.

Admission options:

- Contact group: set `allow_group_id` to a stable ID included in `feishu.group_ids`.
- Departments: put explicit `open_department_id` values in `allowed_department_ids`. Descendant users qualify. Use `feishu.group_ids: []` and `allow_group_id: ""` if Contact-group permission is unavailable.
- Both: matching either selection grants the admission alias `employees/tailnet-members` (organization/name configurable).

The worker requests paginated normal Contact groups through `group/simplelist`. It imports selected groups only, with direct users and fully visible department members expanded. Dynamic groups must first be shown to appear in this API with complete member data in your tenant; the worker fails if a configured group is unavailable. Chat groups and functional roles are outside this adapter.

Set `headplane_role_groups` to a map from selected Contact group IDs to roles, for example `{"g_network_admins": "network_admin"}`. Add every mapping's key to `feishu.group_ids`. If several roles match, precedence is `admin`, `network_admin`, `it_admin`, `auditor`, `viewer`, `member`. This is an explicit deployment policy: split groups or change mappings if that precedence does not fit your organization. Only admitted active users receive a mapped role; everyone else is assigned `member`. The worker never assigns `owner`. Ordinary employees must be unable to change `headplane_role` through Casdoor profile editing. Configure both OIDC applications to emit the needed standard claims and the flat, qualified group IDs; do not assume the default JWT token format is compatible. See `docs/identity-contract.md`.

The worker manages the two `feishu-department-`/`feishu-group-` prefixes plus the admission group. Do not use these names for local groups. It refuses to overwrite a same-name group without `properties.feishu_sync_owner: "tailscale-feishu-integration/v1"`. Existing local groups and other user properties are preserved. All managed groups are Virtual, even when their parent links represent departments. The total serialized group array is bounded to 1000 bytes for the pinned Casdoor claim model; reduce the selected roots/groups if that limit is exceeded.

State records the source configuration, scope hash, known immutable Casdoor IDs, missing counters, pending Headscale revocations, and last successful application. Back it up with Casdoor. A changed app, tenant label, roots/group selections, admission selection, Casdoor origin/organization, or Contact permission scope stops reconciliation. To rebaseline: stop the worker; back up state/Casdoor; review the old and new permission range and identities; archive the old state under a dated name; run a dry run and review the report; apply once; verify before resuming the schedule. Archiving state resets absence history, so review offboarding of users no longer in the new scope separately. Do not automate rebaselining on failure.

Missing users that were previously successfully synchronized retain their groups on the first complete applied absence and are forbidden after at least two complete applied absences. Users that were never in this worker's source scope are left alone. Inactive source status blocks after a complete read. A failed source read makes no Casdoor writes. Reduced permissions stop the run even if the API returned HTTP 200.

`allow_reenable: false` is the default. After an offboarding review, operators may re-enable a user in Casdoor and clear the worker block marker. With `allow_reenable: true`, the worker clears only blocks carrying `properties.feishu_sync_forbidden`; `properties.feishu_sync_hold: "true"` always preserves an operator hold. An existing administrator block without a worker marker is never cleared automatically. Directory synchronization controls new authentication. With the `headscale` section configured, a block also expires the user's Headscale nodes and preauth keys (see below); the worker never revokes Headplane sessions, which expire through the 300 second cookie max age. Without that section, expire nodes per the operations runbook.

## Permission preflight

`--permissions` authenticates the app, calls `GET /open-apis/application/v6/scopes`, and prints a `permissions` block with `granted` (scope names whose grant status is 1), `missing` (one entry per required feature, listing every acceptable scope), `missing_scopes` (the first, narrowest scope of each missing feature), `lifecycle_ready`, and `grant_url`. `--audit` includes the same block. Which features are required follows the configuration: Contact groups only when `feishu.group_ids` is non-empty, company-name verification only when `feishu.expected_tenant_name` is set, the descriptive fields only when `employee_profile.enabled`, and the catalogs only when `employee_profile.catalog_lookup`. `lifecycle_ready` is true when directory read, department tree, user name/avatar, department memberships, employment status, and the configured Contact-group and tenant-name features are all granted; the descriptive fields do not affect it. If Feishu refuses the scope query, `checked` is false, `reason` says why, `lifecycle_ready` is absent, and the command still succeeds.

Any one scope per row suffices; the first is the narrowest. "Broad" means `contact:contact:readonly_as_app`, `contact:contact:readonly`, or `contact:contact:access_as_app`.

| Feature | Data | Scopes |
| --- | --- | --- |
| Directory read | Contact users and `contact/v3/scopes` | `contact:contact.base:readonly`, or broad |
| Department tree | `departments/{id}/children` | `contact:department.base:readonly`, or broad |
| User name/avatar | `name`, `avatar` | `contact:user.base:readonly`, or broad |
| Department memberships and 直属上级 | `department_ids`, `leader_user_id` | `contact:user.department:readonly`, or broad |
| Employment status, required for lifecycle, admission, and offboarding | `status.is_activated`, `is_frozen`, `is_resigned`, `is_exited` | `contact:user.employee:readonly`, or broad |
| 职务 and enterprise email | `job_title`, `enterprise_email` | `contact:user.employee:readonly`, or broad |
| 工号 | `employee_no` | `contact:user.employee_number:read`, `contact:user.employee:readonly`, or broad |
| Email | `email` | `contact:user.email:readonly` |
| 手机号 | `mobile` | `contact:user.phone:readonly` |
| Company name verification, only with `feishu.expected_tenant_name` | `tenant/v2/tenant/query` | `tenant:tenant:readonly` |
| Contact user groups, only with non-empty `feishu.group_ids` | `group/simplelist`, member `simplelist` | `contact:group:readonly`, or broad |
| Job level/family catalogs, only with `employee_profile.catalog_lookup` | `job_levels`, `job_families` | `contact:job_level:readonly` and `contact:job_family:readonly` |

The worker does not use the Directory API (`directory:employee:read` family); grants there do not expose status or job title to the Contact v3 calls. `grant_url` has the form `https://open.feishu.cn/app/<app_id>/auth?q=<scopes>&op_from=openapi&token_type=tenant`, the same link Feishu prints in its 99991672 permission errors. Opening it in the developer console requests exactly the listed scopes; the app version must then be published and approved by the tenant administrator before the tenant access token carries them. Rerun `--permissions` afterwards. As an example only: against one Fysics tenant the preflight reported exactly two missing scopes, `contact:user.employee:readonly` and `contact:user.phone:readonly`, plus `tenant:tenant:readonly` once company-name verification was wanted. Other tenants differ.

## Lifecycle modes

`lifecycle_mode` is `"strict"` by default. Every run reads the complete directory first and then checks whether each source user carries a complete `status` object.

- `strict`: if any user lacks complete status, the run fails with an error naming the missing scope and the grant link, and writes nothing.
- `staged`: when status is unavailable, the run performs the native Casdoor import (creating or updating users and the native profile columns) plus the descriptive employee properties when `employee_profile.enabled`, and reports `mode: apply-staged` (or `dry-run-staged`), `lifecycle: skipped`, `lifecycle_reason`, `users_without_status`, `missing_scopes`, `grant_url`, `unlinked_source_users`, `matched_users`, `native_import`, `profile_updates`, and `revocation`. It never assigns groups, the admission alias, `headplane_role`, or `isForbidden`, never advances missing-user counters, and does not write the state file, except to record the retry of pending Headscale revocations. Source-binding and Contact data-scope checks still apply, so a changed scope stops a staged run as well.

As soon as every source user has complete status, the same configuration performs full lifecycle synchronization (`mode: apply`) on the next run, with no configuration change or restart. Use `staged` for the initial rollout while scope grants are pending and set `strict` once lifecycle has been verified, or keep `staged` if you accept the automatic upgrade. In staged mode new employees are created automatically but cannot enroll devices, because the admission alias is assigned only by full runs.

## Existing-access revocation

The optional `headscale` section makes a block also revoke enrolled devices:

```json
"headscale": {
  "base_url": "http://headscale:8080",
  "api_key_file": "/run/secrets/headscale_api_key",
  "issuer": "https://login.example.com",
  "on_block": "expire",
  "revoke_preauth_keys": true
}
```

`issuer` must equal the Casdoor OIDC issuer Headscale is configured with: Headscale's provider identifier is `issuer/subject`, and the worker matches it against `issuer/<Casdoor user id>`. `on_block` is `expire` (default) or `delete`; `revoke_preauth_keys` defaults to true. When a full applied run sets `isForbidden: true` on a user (inactive status or confirmed missing), the worker looks up every Headscale user whose `providerId` equals that value, expires (or deletes) each of their nodes through the Headscale REST API, and expires their preauth keys. Reports carry `revocation: {configured, subjects, pending, subjects_revoked, headscale_users, nodes_expired, nodes_deleted, preauth_keys_expired}`.

If Headscale is unreachable or answers with a malformed response, the Casdoor block stays applied, the subject is stored in state `pending_revocations`, the run ends with an error, and the revocation is retried on every following applied run, including staged runs, until it succeeds. If `headscale` is omitted, `revocation.configured` is false, `pending` counts the blocked subjects whose devices still need the manual runbook, and nothing is stored.

The API key file is the same file Headplane mounts, and the key expires (90 days in the deployment guide). After a rotation, replace the mounted file and restart both Headplane and the worker. Revocation acts on Headscale only; Headplane sessions still expire only through the 300 second cookie max age.

## Immediate offboarding

`--offboard employees/NAME` prints the planned Casdoor changes; `--offboard employees/NAME --apply` blocks the account (`isForbidden: true`), sets `properties.feishu_sync_hold: "true"` so no later run re-enables it, sets `headplane_role` to `member`, removes the worker-managed groups (the admission alias and the `feishu-*` groups; local groups are preserved), and, when `headscale` is configured, revokes the user's nodes and preauth keys immediately. The target must be `organization/name` inside the configured organization. The report lists `casdoor_updates` and `revocation`; the command is one-shot and does not touch the state file. Operators still review routes, API keys, Casdoor sessions, and Headplane sessions per the operations runbook. Clear the hold only after the review that would justify re-enabling.

## Heartbeat and health check

After each successful applied interval in any mode (`apply`, `apply-staged`, or `profile-only-apply`) the worker writes `<state_file>.heartbeat.json` with `completed_at`, `mode`, and `lifecycle`. The Compose healthcheck reads that file and marks the worker unhealthy when no success occurred within three `interval_seconds`. A staged run counts as a success for the heartbeat; watch `lifecycle` in the reports if admission and offboarding must be live.

Run the local fixture suite:

```sh
python3 -m unittest discover -s tests/worker -v
```

The suite covers transport retry behavior, cursor pagination, complete/partial snapshots, immutable identity, local-group preservation, renames/moves, Contact department expansion, role demotion, safe offboarding, re-enabling/holds, the scope preflight, staged population without status, Headscale node/preauth key revocation with its retry, and immediate offboarding. Live Feishu OAuth, actual field permissions, Casdoor database enforcement, OIDC claim signatures, the Headscale REST API, and application registration still require the deployment acceptance test using real app credentials and approved data scope.

Missing status never permits lifecycle synchronization: `lifecycle_mode: strict` fails the run and `staged` limits it to the native import and descriptive properties. Use `--permissions` to see which scopes are missing, `--audit` for availability counts, or `--profile-only --apply` for descriptive properties on existing users without native imports or access changes. Enable Phone in the native mapping only after `--permissions` shows `contact:user.phone:readonly` granted and the audit confirms `mobile` coverage.
