# Tailnet Stack deployment boundaries

Start with the [provider-neutral deployment guide](../docs/integrated-platform.md).
The core is Headscale, Headplane, Casdoor, PostgreSQL, and Caddy. The Feishu
worker belongs to the optional `sync` Compose profile; the renderer does not
require Feishu credentials. New deployments use project `integrated-tailnet`;
existing runtime project names and image pins are preserved on rerender.


Keep environment-specific configuration, credentials, TLS private keys, and
database backups outside the source checkout. Templates in these component
directories should refer to mounted secret files where the service supports them.
Render configuration containing secrets into a host directory with restricted
permissions when an upstream service requires secrets inside its configuration.

The deployment should persist Headscale's database and private keys, Headplane's
database/session state, Casdoor's database and signing material, sync reconciliation
state, and the proxy's certificate state. Use one active Headscale instance with
its own persistent SQLite database; do not run multiple writers on shared storage.

Publish only the intended HTTPS entry points and any explicitly selected DERP
transport ports. Keep databases, metrics, administration APIs, and service-to-service
ports private. The proxy must forward the original scheme and host correctly for
OAuth redirects. Define exact public URLs and callback URLs in one environment
configuration before generating each service's config.

Do not give Headplane a Docker socket solely to restart Headscale. Begin with
configuration ownership and restarts under operator control; enable broader service
integration only if its capabilities are needed and reviewed. Restrict access to
Headplane until the initial owner account is established.

Backup and restore are part of deployment, not only an upgrade step. Capture a
consistent Headscale database snapshot with its keys, each other service's state,
the release manifest, and encrypted secret material. An offline snapshot taken
after stopping writers is a simple consistent option for this team size. Test a
restore into an isolated environment before allowing traffic. Do not copy a live
SQLite main database while ignoring its WAL.

Identity discovery must establish Casdoor's exact issuer, client behavior, claims,
and the chosen provider's identifier mapping before runtime templates are declared ready. Treat
local image tags as candidates until their deployed digests and end-to-end tenant
test results are recorded in the release manifest.
