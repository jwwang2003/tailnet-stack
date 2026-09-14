# Integrated candidate: neutral branding and bilingual UI

This candidate introduces provider-neutral platform/release/image defaults,
optional Feishu integration, and English/Simplified Chinese UI controls.
The authoritative product source tuple is `versions.lock.yaml`.

## Implementation and checks

- Headplane: request-scoped server-rendered locale, persistent language cookie,
  explicit translations across principal pages/dialogs, role/filter/error/date
  text, and SSH controls. Typecheck, production build, and 227 unit tests passed.
  Seven unit tests cover locale negotiation, interpolation, fallback, SSR
  isolation, and relative time. Independent review found no remaining blocker.
- Casdoor: retained native i18next/language controls; corrected Chinese job-title
  and profile labels and localized the language button's accessible name.
  Typecheck, lint, production build, and catalogue coverage checks passed.
- Headscale: downstream version override, explicit bilingual onboarding pages,
  persistent in-page language switch without OAuth replay, and escaped dynamic
  form attributes. Version tests, template tests, and focused authentication and
  confirmation tests passed. Four synthetic browser flows verified switching,
  unchanged form data, no navigation, and persistence.
- Integration: 120 tests passed, shell syntax and diff checks passed, and all
  clean product checkouts match the source lock. Tests cover neutral defaults,
  optional worker startup, legacy project/image preservation, and strict legacy
  and neutral offline image alias/source validation.

## Local WSL verification

Rebuilt and started the pinned Headscale, Headplane, and Casdoor images locally,
retaining database volumes, account/application IDs, OAuth callbacks, session
secrets, and the existing enrolled device. Headscale health passed and its CLI
reports `v0.29.3-integrated.1`.

Casdoor browser checks passed for English → Chinese → English switching and
Chinese persistence after reload. The employee organization permits `en`/`zh`,
and the local application display name is `Fysics Integrated Platform`.

Headplane browser checks passed for Chinese initial rendering, English switch,
navigation and reload persistence, Chinese cookie-driven server rendering, and
no browser runtime errors. The unauthenticated logout page was used to avoid
replaying the configured automatic Feishu login. Authenticated administration
flows are covered by the unit suite and source review; these browser checks are
not a fresh end-to-end Feishu authentication or authorization acceptance test.

Employee data, raw backups, screenshots, and credentials stay in protected local
runtime storage. No personal employee records are included in this report.

## Release boundaries

Historical branches, commits, logs, repository URLs, and archived bundles remain
intact. New `downstream/integrated-2026.09` and
`release/integrated-2026.09-rc.1` branches identify the candidate. No upstream
main branch was changed and no production promotion is asserted.

Feishu remains one OAuth provider; its optional directory worker remains a
Feishu-specific adapter. Existing unavailable directory-field/status permissions
are not resolved by branding or translation work. Installation documentation,
unknown diagnostics, user-entered data, and protocol/command text may remain in
their original language. Each product remembers its own language preference.

The release remains `compatibility_verified: false` pending the full deployment
acceptance gates. Export a new source-matched bundle for remote deployment;
historical bundles do not contain this interface and must not be relabeled.
