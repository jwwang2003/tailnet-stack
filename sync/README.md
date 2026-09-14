# Feishu directory worker

The worker adds department/group memberships and offboarding to the corrected native Casdoor Lark importer. It uses Python 3.11+ and the standard library. Run it as one replica against a persistent state directory.

```sh
python3 sync/worker.py --config /path/to/sync.json
python3 sync/worker.py --config /path/to/sync.json --apply
python3 sync/worker.py --config /path/to/sync.json --apply --watch
```

The first command is a read-only dry run. `--apply` also runs the native profile importer, applies the reviewed reconciliation, and atomically writes state. `--watch` repeats at `interval_seconds`; a failed interval logs an error and retries at the next interval. One-shot failures exit nonzero. Every success emits a JSON report with source counts, unmatched users, pending missing users, blocks/re-enables, operation count, completion timestamp, and scope hash. Reports contain no tokens, user profiles, or secrets.

Start from `config.example.json`. Mount credentials as files and keep the populated config private. `feishu.base_url` selects `https://open.feishu.cn` or `https://open.larksuite.com`; the native syncer's `host` and `user` must match that origin and app ID. The configured tenant key binds durable state to the operator's intended tenant; it is not API-attested tenant verification. The enterprise app's ownership and authorized data range are the actual directory trust boundary.

`casdoor.client_id`/secret must belong to a dedicated, restricted server-side application able to read the selected organization's users/groups and syncer, invoke that syncer, create/update groups, and update user groups/properties/status. Basic authentication keeps the secret out of URLs. Keep plain HTTP Casdoor calls on the private container network; use HTTPS for remote hosts. The worker refuses HTTP redirects, bounds retries and response sizes, and does not retry uncertain writes automatically.

Configure Casdoor's native Lark syncer with `isEnabled: false`, the matching organization and Feishu app, and profile-only `tableColumns`. Example:

```json
[
  {"name": "Lark", "casdoorName": "Lark", "isKey": true, "isHashed": false},
  {"name": "DisplayName", "casdoorName": "DisplayName"},
  {"name": "Email", "casdoorName": "Email"},
  {"name": "Avatar", "casdoorName": "Avatar"},
  {"name": "Title", "casdoorName": "Title"}
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

State records the source configuration, scope hash, known immutable Casdoor IDs, missing counters, and last successful application. Back it up with Casdoor. A changed app, tenant label, roots/group selections, admission selection, Casdoor origin/organization, or Contact permission scope stops reconciliation. To rebaseline: stop the worker; back up state/Casdoor; review the old and new permission range and identities; archive the old state under a dated name; run a dry run and review the report; apply once; verify before resuming the schedule. Archiving state resets absence history, so review offboarding of users no longer in the new scope separately. Do not automate rebaselining on failure.

Missing users that were previously successfully synchronized retain their groups on the first complete applied absence and are forbidden after at least two complete applied absences. Users that were never in this worker's source scope are left alone. Inactive source status blocks after a complete read. A failed source read makes no Casdoor writes. Reduced permissions stop the run even if the API returned HTTP 200.

`allow_reenable: false` is the default. After an offboarding review, operators may re-enable a user in Casdoor and clear the worker block marker. With `allow_reenable: true`, the worker clears only blocks carrying `properties.feishu_sync_forbidden`; `properties.feishu_sync_hold: "true"` always preserves an operator hold. An existing administrator block without a worker marker is never cleared automatically. Expire existing Headscale nodes and revoke Headplane sessions separately; directory synchronization controls new authentication.

Run the local fixture suite:

```sh
python3 -m unittest discover -s tests/worker -v
```

The suite covers transport retry behavior, cursor pagination, complete/partial snapshots, immutable identity, local-group preservation, renames/moves, Contact department expansion, role demotion, safe offboarding, and re-enabling/holds. Live Feishu OAuth, actual field permissions, Casdoor database enforcement, OIDC claim signatures, and application registration still require the deployment acceptance test using real app credentials and approved data scope.
