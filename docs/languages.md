# English and 简体中文

Integrated Tailnet retains each upstream application's identity. Its language
controls translate interface text, not employee names, device names, policy code,
provider names, or other administrator-entered data. English is the fallback for
new upstream messages that do not yet have a translation. Feishu remains a named
optional OAuth/directory provider; choosing Chinese does not select a provider.

## Headplane

Use **Language / 语言** in the bottom-right corner of the login and administration pages to choose
**English** or **简体中文**. The interface changes without signing out. A validated
`headplane_locale` cookie remembers the choice for one year; fresh sessions use
the browser's preferred supported language, then English. Server rendering uses
the same preference as the client, including the document language.

The translation catalogue covers navigation, machine and user management,
settings, access controls, dialogs, login, notifications, and SSH controls.
Terminal output, backend diagnostics, and policy/command syntax retain their
original content. Language selection grants no administrative permissions.

## Casdoor

Use the language menu in the console or sign-in page. Configure the employee
organization's **Languages** as `en` and `zh` (Casdoor uses `zh`, not `zh-CN`). Keep
the language widget enabled and the application's **Languages** sign-in/signup
item visible. Casdoor already supplies its own translation framework and saves
selection in `localStorage.language`. Its user **Job title** label is **职务**.

Organization and application display names are literal configuration values.
Use neutral names such as **Fysics Integrated Platform**, and keep the OAuth
provider label **Feishu / 飞书** where that provider is selected. Update display
names rather than immutable organization, application, or account IDs.

## Headscale device onboarding

Registration instructions, device confirmation, and authentication result pages
provide an English/简体中文 selector. It switches explicitly rendered translations
without a page reload, network request, or form submission, so it cannot replay
an OAuth callback. Device details and the CSRF-protected confirmation form remain
unchanged. Without JavaScript, the original English interface remains usable.

Platform installation documentation pages and previously unknown diagnostic
messages can remain English. A language preference is local to each application's
mechanism; the three applications do not change one another's saved selection.

## Maintaining translations

Keep UI labels separate from API values and identifiers. In Headplane, add a
source message and its Chinese translation to `app/i18n`, and use the explicit
translation helpers in components. Preserve placeholders and translate complete
sentences rather than concatenating independently translated fragments. Keep
locale negotiation, server rendering, and language-switch tests passing.

Casdoor translations live in its existing `web/src/locales` catalogues. Headscale
onboarding translations live in `hscontrol/templates/auth_locale.go`. Retain
English fallback for new upstream text, review catalogue coverage on upgrades,
and never translate or rewrite arbitrary employee data or HTML after rendering.

Rebuild the affected product images and export a new matching bundle after source
changes. A historical bundle still contains the old interface and must retain
its original source manifest.
