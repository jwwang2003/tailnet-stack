# Employee handbook

This handbook describes device enrollment through the company’s configured sign-in provider. Feishu is one supported option. Before distributing it, IT must replace the example addresses and complete the client-version table during the pilot.

| Item | Your team's value |
| --- | --- |
| VPN coordination server | `https://vpn.example.com` |
| Sign-in service | `https://login.example.com` |
| Administration UI (authorized staff only) | `https://admin.example.com/admin/` |
| Example approved resource | IT supplies this during onboarding |
| Support / lost-device contact | IT supplies this before rollout |
| Tested Windows / macOS / Linux / Android / iOS versions | Pending pilot; use IT-approved versions |

## First connection

1. Ask IT to confirm that your Feishu account belongs to the approved VPN group. A department change may affect which resources you can use.
2. Install the official Tailscale client from [Tailscale downloads](https://tailscale.com/download). Choose the version approved by IT. This deployment's Headscale baseline requires at least Tailscale 1.80.0; that minimum alone is not a tested compatibility recommendation.
3. Set the custom coordination server to `https://vpn.example.com` using the instructions for your device below. Standard Tailscale cloud login uses a different server.
4. In the browser, choose the company’s configured provider (for example, Feishu) at the team's sign-in service. Confirm the correct company/account and consent only to the team's published application.
5. Finish the browser's device-registration confirmation and return to Tailscale. Confirm it shows Connected.
6. Open the approved resource IT supplied. IT will also provide a resource you should be unable to reach as an onboarding permissions check.

Never send login links, authorization codes, or session cookies to a colleague. Each employee signs in with their own account.

## Device-specific setup

### Windows

Install the official client. Open PowerShell and run:

```powershell
tailscale login --login-server https://vpn.example.com
```

Complete your company sign-in in the browser. If your computer's policy blocks setup, ask IT; do not change corporate security settings. See `https://vpn.example.com/windows` for the server's version-specific client instructions.

### macOS

Use the approved Tailscale client variant. Option-click the menu-bar icon, choose Debug → Custom Login Server → Add Account, and enter the VPN address. If the CLI is installed, the Windows login command above also applies. See `https://vpn.example.com/apple` for the server's instructions.

### Linux

Install Tailscale using your distribution's approved installation procedure. Then:

```sh
sudo tailscale login --login-server https://vpn.example.com
tailscale status
```

Open the login URL shown by the command on a browser accessible to you. Keep the URL private.

### Android

Open Tailscale → Settings → Accounts → three-dot menu → Use an alternate server. Enter the VPN address and complete browser login. Allow the OS VPN permission when prompted.

### iOS

Open Tailscale → account icon → Log in → options menu → Use custom coordination server. Enter the VPN address and complete login. Allow the VPN configuration when prompted.

Menu labels can change between client releases. These steps derive from the selected Headscale release's [client guides](https://github.com/juanfont/headscale/tree/v0.29.3/docs/usage/connect); IT must verify the actual pilot versions before distributing this handbook.

## Daily use

Use Connect/Disconnect in the Tailscale client. Prefer a recognizable device name such as `jwei-work-laptop`; tell IT the displayed name if it needs changing. On Linux, `tailscale status` helps identify connectivity issues.

The VPN gives access only to approved resources. Department membership does not automatically grant access to every machine in that department. Your usual internet traffic keeps its normal route unless IT explicitly configures an exit node. Do not advertise subnet routes, enable an exit node, or run a subnet router without IT's instructions.

Device authentication defaults to 30 days in the supplied deployment configuration. When the client asks you to sign in again, repeat the Feishu login flow. Logging out of the web administration UI does not disconnect your VPN device.

Headplane is primarily for authorized administrators. Ordinary employees may have no UI access; a denied Headplane page does not necessarily mean the VPN is broken.

## Getting help

| Symptom | What to check |
| --- | --- |
| Login opens Tailscale cloud | Confirm the custom coordination-server URL. |
| Feishu denies consent | Confirm the company account and ask IT to check application availability. |
| Access denied after login | Ask IT to check your VPN group and synchronization status. |
| Connected but a resource fails | Check the supplied hostname, normal internet connection, and whether that resource is approved. |
| Device expired | Reauthenticate in the client. |
| Headplane says no access | Confirm that your role includes administration UI access. |

When contacting support, provide your OS/client version, device name, time of failure, and the error text. Redact login URLs and tokens. Never provide app secrets or API keys.

Report lost or stolen devices immediately so IT can revoke their access. Disconnecting another device or signing out of Feishu is not a substitute for revocation. When leaving the company, follow IT's device-return procedure; access is revoked centrally.

## Interface language

Headplane and the device-registration pages offer **Language / 语言 → English / 简体中文**. Casdoor has its own language menu. Each service remembers its selection independently; switching language does not change your account or access. See [language settings](languages.md).
