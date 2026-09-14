# Feishu → Casdoor → tailnet identity contract

This is the integration's compatibility contract. Test it against the exact release tuple in `versions.lock.yaml` before admitting production users. The examples are synthetic; they are not captured credentials or tokens.

## Identity and trust boundaries

Use one Feishu **custom enterprise app**, one Casdoor organization, and separate confidential OIDC applications for Headscale and Headplane. Login uses the Casdoor Lark OAuth provider with the Feishu endpoint selected. Directory reads use the **same app ID and secret**. Changing apps is an identity migration, because `open_id` is app-specific.

| Value | Contract |
| --- | --- |
| Feishu `open_id` | Required, nonempty binding for both OAuth and directory import. Store in Casdoor `lark`. Never substitute email, department, `user_id`, or `union_id` in this field. |
| Feishu `user_id`, `union_id` | Optional metadata. Availability depends on permissions. Never merge users solely by these fallback values. |
| Feishu `tenant_key` | Pin the intended tenant in app configuration and acceptance evidence. App credentials and approved directory scope define the worker's source. A configuration label is not proof that an OAuth login is from that tenant. |
| Casdoor `owner/name` | Casdoor API record address. Preserve the existing record when linking OAuth and import. |
| Casdoor `id` | Immutable user identity. It becomes OIDC `sub`; never regenerate it on profile edits, sync, or re-login. |
| OIDC `iss` | Stable public HTTPS Casdoor origin, matching discovery and token verification. Changing it migrates the Headscale identity. |
| Headscale provider ID | Derived from issuer and subject. Check repeated login and both clients match the same user. Do not rely on email fallback for Headplane linking. |

The checked Casdoor Lark provider binds `UserInfo.Id` to `open_id`; its native syncer originally stored `user_id` in `lark`. The downstream Casdoor fix must make these agree and reject an empty binding. Do not run the uncorrected native syncer in production. Do not automatically link an existing email to a different Feishu ID. `scripts/casdoor-harden.py` sets every provider's `bindingRule` to `[]` on the employee applications, so an unlinked Feishu login is refused (the account does not exist and is not allowed to sign up) instead of being attached by email, phone, or username; the worker and the native import link by `lark` open_id only.

## Downstream claim contract

| Claim | Required behavior |
| --- | --- |
| `iss` | Exact configured issuer. |
| `sub` | Same nonempty Casdoor user ID for both applications; cannot contain `/`, because Headplane matches the final path component of Headscale's provider ID. |
| `aud` | Correct client ID for the receiving application. |
| `exp`, `iat`, `nonce` | Valid OIDC timestamps and the client-supplied nonce. |
| `name` / `preferred_username` | Display/profile fields; not identity keys. Verify the selected Casdoor token format emits the desired standard names. |
| `email` | Optional, mutable profile data. Prefer the enterprise email for the displayed directory profile. |
| `email_verified` | Boolean; set true only with a justified verification policy. Do not fabricate verification to make email-based authorization work. |
| `groups` | Array of exact qualified Casdoor group IDs such as `employees/feishu-group-g_tailnet`. Keep `useGroupPathInToken=false`, so department moves and display renames do not change authorization strings. Check ID token **and** UserInfo output. |
| `headplane_role` | Optional single value: `member`, `viewer`, `auditor`, `it_admin`, `network_admin`, `admin`. Default to `member` (no UI access). Never emit `owner`; bootstrap the owner in a controlled first login. |

Use authorization code flow and PKCE S256. Enable standard signing-key validation; do not enable weak RSA compatibility. Disable ordinary users' ability to edit group memberships, the `lark` link, identity IDs, or role claims. `scripts/casdoor-harden.py` applies that policy to the organization's account items, and `scripts/verify-casdoor-authz.py` proves it against the deployed image: the pinned downstream Casdoor commit `fde2cf8f` (User.DeepCopy in UpdateUser) closes the partial-update bypass through which an ordinary user of unfixed v4.3.0 could set `properties.headplane_role` and replace `groups`. Memberships do not map automatically to Headplane role names: any admin role mapping must be separately reviewed.

## Groups and lifecycle ownership

Casdoor group names cannot contain `/`. The slash exists only in the qualified API/token identity (`owner/name`). Store hierarchy in `parentId` and labels in `displayName`.

| Feishu structure | Casdoor name | Owner |
| --- | --- | --- |
| Department `od_engineering` | `feishu-department-od_engineering` | Worker; stable name, mutable display name/parent. |
| Contact user group `g_tailnet` | `feishu-group-g_tailnet` | Worker; virtual group. |
| Local Casdoor group | Any name outside the two reserved prefixes | Operator; worker preserves it. |
| Users and profile fields | Existing account linked by `lark == open_id` | Corrected native Casdoor syncer, invoked by the worker in full and staged runs. The worker itself does not create users. |
| Feishu memberships | User `groups` entries under the reserved prefixes, plus the configured admission alias (default `tailnet-members`) | Worker. Native syncer must preserve them. |
| Source inactive or confirmed missing users | `isForbidden` plus worker marker in `properties` | Worker; native import may initialize a new user's inactive status. Operator hold always wins. |
| Headplane role | `properties.headplane_role` | Worker, from explicitly configured Contact group IDs; default/demotion is `member`. |
| Headscale nodes and preauth keys of a blocked user | Headscale users whose provider ID is `issuer/<Casdoor id>` | Worker, when its `headscale` section is configured; otherwise operator, per the runbook. |

Department memberships include ancestors within the configured roots. All synchronized groups are Casdoor **Virtual** groups, including the department hierarchy, because Casdoor permits only one Physical group per user. Contact user groups are explicitly selected by stable group ID; they are distinct from Feishu chat groups. A Contact group department member expands to users in that department and its fully read descendants. Out-of-scope department members stop the run. The Feishu API uses `member_id_type=open_id` with `member_type=department` to request `open_department_id` values, as defined in the [official Contact SDK](https://pkg.go.dev/github.com/larksuite/oapi-sdk-go/v3/service/contact/v3#MemberIdTypeSimplelistGroupMemberOpenId). Chat membership, functional roles, custom attributes, and other structures require separate adapters and are not silently treated as authorization groups.

Admission is the union of the configured Contact group and `allowed_department_ids`. Department-only deployment can set `feishu.group_ids: []` and `allow_group_id: ""`, which performs no Contact-group reads. The worker materializes `employees/tailnet-members` for admitted active users. Headscale's `allowed_groups` should reference that qualified alias. A role mapping never grants tailnet admission by itself.

Disable the native syncer scheduler and set `isReadOnly: true` to prevent unsupported writes back to Feishu. Configure explicit `tableColumns` with exactly one `isKey: true` column, `Lark`, and `isHashed: false` on that key. Set `isHashed: true` on every profile field so native change detection includes it. Other columns are profile fields, with matching Casdoor-cased `name` and `casdoorName` (for example `DisplayName`/`DisplayName`); exclude `Name`, `Groups`, `Properties`, `IsForbidden`, and immutable `Id`. Matching casing is this integration's configuration convention. The worker checks the native syncer's organization, Feishu origin/app ID, scheduling, key, and field ownership before invoking its synchronous `run-syncer` endpoint during an applied run. Dry runs never invoke it. Do not schedule native imports separately or edit owned fields while a worker run is applying. The downstream native import logs only owner/name identifiers, and its Lark pagination rejects empty or repeated cursors and more than 10000 pages.

Read all pages of all configured departments and groups before planning writes. Reject errors, permission failures, repeated cursors, missing identifiers, inconsistent duplicate records, and a changed permission-scope snapshot. A partial run must not remove memberships, disable users, or advance the missing-user counter. A missing user is marked on one complete applied run and blocked only after a second complete applied run. Explicit inactive status can block after a complete read. Retain accounts and groups; do not delete them automatically. Missing employment status never permits lifecycle changes: `lifecycle_mode: strict` fails the run, and `staged` limits it to the native import and descriptive properties until every user carries complete status, after which the same configuration performs full runs.

Store the worker's source binding and permission-scope fingerprint in durable state. A changed app, organization, root/group selection, or permissions requires an operator-reviewed rebaseline; a valid but narrower API scope is not evidence of employee departure. Dry runs do not change Casdoor or state.

The worker preserves a pre-existing administrator block. It records its own block with `properties.feishu_sync_forbidden`; `allow_reenable` defaults to false. If explicitly enabled, a now-active user can be re-enabled only when that worker marker exists; re-enabling clears it together with `feishu_sync_revoked`. Operators can set `properties.feishu_sync_hold: "true"` for an unconditional hold. New imported users already marked forbidden by native import require operator review before re-enabling, because their initial block has no worker ownership marker.

Blocking Casdoor prevents new authentication. When the worker's `headscale` section is configured, a full applied run that sets `isForbidden` also expires (or deletes) the Headscale nodes whose provider identifier is `issuer/<Casdoor id>` and expires that user's preauth keys. The subject is journaled in `<state_file>.revocations.json` before the block and the user is marked `properties.feishu_sync_revoked` after Headscale confirms, so every later applied run, full or staged, completes a revocation that failed or was interrupted, and any linked user forbidden with `feishu_sync_forbidden` or `feishu_sync_hold` but no revoked marker is revoked on the next applied run. An administrator block without a worker marker is never revoked automatically; a journaled block keeps its entry even if the marker is removed by hand, and a new block clears a stale revoked marker. `--offboard --apply` does the same immediately for one account under the worker's state lock and journals it the same way. Without that section, blocking does **not** revoke existing nodes, and offboarding must expire/delete the employee's nodes using the operator procedure. Neither path revokes Headplane sessions, which expire through the cookie max age; revoke UI sessions using the operator procedure. OIDC admission `allowed_groups` does **not** create Headscale ACL policy groups.

## Feishu app permissions and acceptance gates

App developers can implement this without using Feishu enterprise SAML administration. Reading organization data still requires the app's Contact permissions and authorized directory range; creating an app does not grant organization-wide access. Have the app publisher/appropriate approver grant only the intended employee population and required user, department, scope, and Contact-group read access. Verify actual returned `open_id`, status fields, enterprise email, and group member types before enabling writes. The worker's `--permissions` preflight lists the granted and missing Contact scopes per feature with a grant link; the exact table is in the worker guide.

Keep both Casdoor applications unavailable to ordinary employees until these checks pass:

1. Sync-before-login and login-before-sync result in one Casdoor record, one immutable `id`, and one matching `lark` link.
2. Changed email/name, absent `user_id`, and duplicate email with a different `open_id` preserve identity isolation.
3. Both application tokens pass OIDC validation and have the same `sub`, correct different audiences, stable qualified groups, and no accidental admin role.
4. Nested/multiple department membership, department rename/move, Contact group changes, and interrupted pagination converge on repeated sync.
5. Reduced permission scope produces an error with zero mutations; a genuinely absent user blocks only after two completed applied snapshots.
6. Disabled, re-enabled, and manually held users follow the documented ownership policy. Existing tailnet revocation is exercised: automatically when `headscale` is configured, manually otherwise. Session revocation is exercised separately.
7. `--permissions` reports `lifecycle_ready: true` for the production app, and `scripts/verify-casdoor-authz.py` passes against the deployed Casdoor image.

Run local fixture checks with `python3 -m unittest discover -s tests/worker -v`. These are contract checks, not proof of a real Feishu tenant login; live gates require an app, approved permissions, TLS/DNS, and credentials supplied at deployment.

## Primary references

- [Casdoor Lark syncer documentation](https://casdoor.ai/docs/syncer/Lark/) describes native import; verify its claims against the pinned source.
- [Casdoor Lark provider source](https://github.com/casdoor/casdoor/blob/master/idp/lark.go), [syncer source](https://github.com/casdoor/casdoor/blob/master/object/syncer_lark.go), [JWT generation](https://github.com/casdoor/casdoor/blob/master/object/token_jwt.go), and [group API implementation](https://github.com/casdoor/casdoor/blob/master/object/group.go).
- [Feishu tenant access token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal), [Contact scope](https://open.feishu.cn/document/server-docs/contact-v3/scope/list), [departments](https://open.feishu.cn/document/server-docs/contact-v3/department/children), and [Contact group membership](https://open.feishu.cn/document/server-docs/contact-v3/group-member/simplelist).
