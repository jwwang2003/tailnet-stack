# Employee directory: 部门、职务、工号

The employee-directory extension keeps business data separate from login identity and authorization. For the current Fysics deployment, the requested company is 飞捷科思. Configure the exact tenant name only after the tenant API can return it; the app ID/source binding alone is not a tenant-name verification.

| Employee detail | Feishu source | Casdoor storage |
| --- | --- | --- |
| 姓名 | `name` | Native `displayName`; descriptive `properties.feishu_name` |
| 部门 | Direct-child traversal + users by department | `properties.feishu_departments`: JSON records with IDs, names and paths |
| 职务 | `job_title` | Native `title`; descriptive `properties.feishu_job_title` |
| 工号 | `employee_no` | `properties.feishu_employee_no`, retained as text including leading zeroes |
| 手机号 | `mobile` | Native `phone` when its column is enabled; descriptive `properties.feishu_mobile` |
| 邮箱 | `email` / `enterprise_email` | Native email with fallback; separate descriptive source properties |
| 直属上级 | `leader_user_id` | `properties.feishu_manager_open_id` |

Do not map 工号 to Casdoor's immutable `id` or `name`: `id` is the OIDC subject and changing it breaks user linking. Casdoor's `Ranking` is an activity/score field, not an employee's rank or job title, and this integration never changes it. Job title, manager status, and optional job level metadata never make an employee a Casdoor/Headplane administrator.

## Permissions and missing values

Feishu may omit fields that the app cannot read. An omitted/null field means **unavailable**, not confirmed empty. The worker preserves prior descriptive values for unavailable fields and records the observed fields in `feishu_profile_available_fields`. An explicitly returned empty string can clear a descriptive value. Counts distinguish availability from population; some employees may legitimately have an empty 工号 or email.

Run the permission preflight before requesting grants; it needs the populated configuration but reads no directory data:

```sh
python3 sync/worker.py --config .runtime/local-wsl/sync/demo-config.json --permissions
```

It prints the granted scopes, `missing` per feature with every acceptable scope, `missing_scopes`, `lifecycle_ready`, and a `grant_url` that opens the request for exactly the missing scopes in the developer console; the app version must then be published and approved by the tenant administrator. The field permissions follow the [Contact Get user / 获取单个用户信息 API documentation](https://open.feishu.cn/document/server-docs/contact-v3/user/get); any one scope per row suffices:

| Employee detail | Feishu field | Scope |
| --- | --- | --- |
| 职务, enterprise email, and employment `status` | `job_title`, `enterprise_email`, `status` | `contact:user.employee:readonly` |
| 工号 | `employee_no` | `contact:user.employee_number:read` or `contact:user.employee:readonly` |
| 邮箱 | `email` | `contact:user.email:readonly` |
| 手机号 | `mobile` | `contact:user.phone:readonly` |
| 部门 memberships and 直属上级 | `department_ids`, `leader_user_id` | `contact:user.department:readonly` |
| 姓名 and avatar | `name`, `avatar` | `contact:user.base:readonly` |

The broad scopes `contact:contact:readonly_as_app`, `contact:contact:readonly`, and `contact:contact:access_as_app` also satisfy every row except 邮箱 and 手机号. The worker does not use the Directory API (`directory:employee:read` family); grants there do not expose status or job title to the Contact v3 calls. A successful API response or basic profile permission does not guarantee those fields are included. As an example only: against the current Fysics tenant the preflight reported exactly two missing scopes for the full field set, `contact:user.employee:readonly` and `contact:user.phone:readonly`, plus `tenant:tenant:readonly` once company-name verification is configured. Recheck your own tenant; the set differs between tenants and changes after each approval. The complete feature table is in the [worker guide](../sync/README.md#permission-preflight).

Additional features:

- Company-name verification requires `tenant:tenant:readonly` and `feishu.expected_tenant_name` matching the returned name exactly.
- Full lifecycle/admission synchronization requires readable user `status` fields for activated/frozen/resigned/exited. It must not infer active employment from a missing status object. With the default `lifecycle_mode: "strict"` a run without complete status fails and writes nothing; with `"staged"` it imports users and descriptive properties, reports `lifecycle: skipped`, and upgrades itself to full synchronization once the status scope is effective. See [lifecycle modes](../sync/README.md#lifecycle-modes).
- Job-level and job-family catalog lookups are optional and are **not required** for 部门、职务、工号. If deliberately enabled, they require `contact:job_level:readonly` and `contact:job_family:readonly`; names are stored separately from IDs.
- Custom HR fields and functional-role memberships are not inferred from licenses, manager status, or department depth. They require a selected, explicit adapter. Unrelated custom attributes are not copied.

## Audit before enabling writes

Run with the populated private configuration:

```sh
python3 sync/worker.py --config .runtime/local-wsl/sync/demo-config.json --audit
```

Audit mode reads the complete permitted directory and reports **counts only**, even when employment status is unavailable. It never calls the native importer, updates Casdoor, or advances offboarding counters. It rejects malformed/incomplete source pagination. Department parent edges come from explicit direct-child API responses, so missing `parent_department_id` no longer requires guessing or additional parent-field permission.

Review `source_users`, `departments`, `direct_memberships`, `users_with_complete_status`, `profile_coverage.fields`, and the embedded `permissions` block (`lifecycle_ready`, `missing_scopes`, `grant_url`). Audit success means the read completed, not that every requested field is available or that synchronization is production-ready.

## Descriptive metadata only

Enable in the private worker configuration:

```json
"employee_profile": {
  "enabled": true,
  "catalog_lookup": false
}
```

Then preview and apply descriptive properties to **existing, already linked** Casdoor users:

```sh
python3 sync/worker.py --config .runtime/local-wsl/sync/demo-config.json --profile-only
python3 sync/worker.py --config .runtime/local-wsl/sync/demo-config.json --profile-only --apply
```

This mode can populate department paths and 工号 while status permissions are still missing. It does not create users, run native imports, change native phone/title/email columns, assign groups or roles, change forbidden status, or advance lifecycle state. It preserves unrelated properties and OAuth credentials, sends only changed owned keys, and detects identity/concurrent-edit conflicts before updates. If accounts must also be created while status is pending, use `lifecycle_mode: "staged"` with the normal dry-run/`--apply` workflow instead; profile-only never creates users.

Inspect an employee in Casdoor **Users → employees → Properties**:

- `feishu_employee_no`: 工号
- `feishu_job_title`: 职务, when returned
- `feishu_departments`: direct department memberships and root-to-leaf names/IDs
- `feishu_profile_available_fields`: fields actually present in the current source read

Use administrator access for full employee-directory inspection; do not expose profile properties publicly or grant employee accounts permission to edit synchronized metadata/access properties.

## Full synchronization

Once `--permissions` reports `lifecycle_ready: true` and the necessary native profile fields are available, use the normal dry-run/`--apply` workflow; a worker already running in `staged` mode switches to full runs by itself. Native syncer columns must include Phone **only when that field is authorized and intended to synchronize**:

```json
{"name":"Phone","casdoorName":"Phone","type":"string","isHashed":true}
```

Keep `Lark` as the sole immutable key and exclude `Id`, `Name`, `Properties`, `Groups`, and `IsForbidden` from native updates. The full worker orchestrates native profile updates, then employee properties, memberships, lifecycle changes, and, when `headscale` is configured, revocation of blocked users' Headscale nodes and preauth keys. It refuses a native update that would erase a populated phone/title/email because the source field disappeared.

The same state-file lock serializes applied profile-only and full runs. Do not run independent native scheduling or change worker-owned attributes concurrently. Full synchronization still uses the existing explicit admission rules and role mapping; business metadata has no implicit authority.

## Privacy and validation

Employee records and raw API responses belong only in protected runtime storage, not in Git, test fixtures, chat logs, or public release notes. Commit synthetic fixtures and aggregate test outcomes. Use mode 0600 for local exports/backups and back up the Casdoor database before a bulk update.

Tests cover missing-versus-empty fields, 工号 leading zeroes, duplicate records, department membership/path reconstruction, catalog name changes, credential preservation, company mismatch, the scope preflight, staged runs without status, and profile-only isolation from authorization. Live field coverage must be rechecked after app permission or source data changes.
