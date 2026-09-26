# Self-hosted deployment

This guide deploys one C-Link instance for multiple operator-provisioned users on a single Linux Docker host. Docker Compose starts C-Link, SQLite storage, and Caddy as a public TLS reverse proxy. It is a useful small deployment baseline; it does not provide high availability or horizontal scaling.

## Requirements

- A Linux host with Docker Engine and the Docker Compose v2 plugin.
- A DNS name whose A/AAAA records point to this host.
- Inbound TCP ports 80 and 443 allowed by the host firewall and provider firewall. Caddy uses port 80 for certificate validation and redirects HTTP to HTTPS.
- An OpenAI-compatible Chat Completions model server reachable from the Docker host over a private or otherwise trusted network. Keep its endpoint and credentials out of public access.
- A plan for encrypted off-host backups and data retention.

The C-Link container is not published to a host port. Only Caddy publishes 80/443. The Compose `backend` network is internal, while Caddy and C-Link share the proxy network. Caddy automatically requests and renews public certificates for a correctly configured domain.

## Configure and start

From the `production` directory on the deployment host:

```sh
cp deploy/.env.example deploy/.env
mkdir -p deploy/secrets
chmod 700 deploy/secrets
```

Edit `deploy/.env` and set:

- `C_LINK_DOMAIN` to the public DNS name.
- `C_LINK_LLAMACPP_URL` to the trusted model server's OpenAI-compatible API base URL, reachable from the C-Link container.
- `C_LINK_UPSTREAM_MODEL_NAME` if the model server expects a model ID other than `local-model`.
- Resource limits appropriate for the host. Defaults are 60 requests per minute per tenant, one concurrent generation, 4,096 output tokens, 2 MiB request bodies, and 180 seconds for provider timeouts.

Create the provider key file if the upstream requires a bearer API key. Keep it out of source control and restrict file permissions:

```sh
umask 077
printf '%s' 'YOUR_MODEL_PROVIDER_KEY' > deploy/secrets/llamacpp_api_key
chmod 600 deploy/secrets/llamacpp_api_key
```

If the upstream is already protected by a trusted private network and does not require a key, create the required empty file instead:

```sh
: > deploy/secrets/llamacpp_api_key
chmod 600 deploy/secrets/llamacpp_api_key
```

Validate and start the stack:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f c-link caddy
```

When DNS and firewall settings are correct, check `https://YOUR_DOMAIN/health` and `https://YOUR_DOMAIN/health/provider`. The health endpoint confirms C-Link and its database are ready; the provider endpoint checks the model server. Interactive API docs are available locally at `/docs`; the supplied public Caddy configuration does not route the docs or schema endpoints.

## Provision users and keys

There is no public signup or tenant administration API. Run tenant management commands on the host through `docker compose exec`; the API key is printed once. Deliver it to the intended user through a secure channel and have them store it in a password manager.

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant create "User or team name"
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant list
```

The create command returns a tenant ID and one `cl_live_...` key. Do not put the plaintext key in shell history, source files, tickets, or logs. For additional independent keys on the same tenant, issue a new key and then revoke the old one:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant key TENANT_ID "laptop-2026"
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant keys TENANT_ID
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant revoke-key KEY_PREFIX
```

Keys for one tenant share that tenant's memory. To give a person separate data, create a separate tenant. Suspend a tenant without deleting its data, or resume it later:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant suspend TENANT_ID
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant resume TENANT_ID
```

Permanent deletion removes the tenant's API keys, sessions, messages, snapshots, and context projections. Confirm the tenant ID carefully before running:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link tenant delete TENANT_ID --confirm
```

## Connect an app

Configure the app's OpenAI-compatible provider with:

- Base URL: `https://YOUR_DOMAIN/v1`
- API key: the key issued for that tenant
- Model: `local-model`

Send a stable conversation/session ID when the app supports it. See [Integrations](INTEGRATIONS.md) for OpenAI SDK, Open WebUI, Continue, and Hermes examples. Keep tenant keys in server-side application configuration; never embed a shared key in public browser code.

All `/v1/*` routes require `Authorization: Bearer <tenant-key>`. Each request is associated with one tenant, and session, message, context, event, snapshot, and retrieval operations are tenant scoped. The `GET /health` endpoint is intentionally public and returns only basic readiness information.

## Backups and restore

Create a consistent SQLite backup inside the persistent Docker volume:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link backup --output /var/lib/c-link/backups/c-link.sqlite3
```

Copy the backup off the host using an encrypted channel and keep access restricted. The backup contains users' conversation and context data; encrypt it at rest and define retention before accepting real user data. The C-Link volume itself is not encrypted by this Compose file.

Restore only after stopping C-Link. Put the backup in a path mounted into the container or copy it into the volume first. Then run the restore command against the same `/var/lib/c-link` volume and restart the service:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml stop c-link
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps c-link c-link restore /var/lib/c-link/backups/c-link.sqlite3
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d c-link
```

The restore utility validates the backup and preserves a safety copy of the current database when available. Test restoration before relying on backups.

## Updates and operations

Review source changes and release notes before updating. Back up first, then rebuild and recreate the C-Link service:

```sh
docker compose --env-file deploy/.env -f deploy/compose.yaml exec c-link c-link backup --output /var/lib/c-link/backups/pre-update.sqlite3
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d --force-recreate c-link
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

Monitor C-Link/Caddy logs, host disk space, provider availability, failed requests, and backup completion. The included health check is a readiness check, not a complete monitoring or alerting system. Do not put API keys, user prompts, or private context in public logs.

## Current limits and launch checks

- One C-Link process and one SQLite database. Multiple containers or hosts are not supported for shared state.
- Per-tenant request counters and generation concurrency limits are in process memory and reset at restart.
- User provisioning is local CLI administration. There is no password login, self-service account portal, organization role system, or billing.
- Conversation data is stored in SQLite without application-level encryption. Protect the host, Docker volume, backups, and provider link.
- The Compose file has been syntax-checked, but image build, container startup, and automatic certificate issuance must be verified on the actual Linux host because they depend on that host's Docker daemon, DNS, firewall, and network.
- Before inviting users, verify domain/TLS, tenant isolation, provider authentication, backups and restore, retention, resource limits, and privacy terms on the target host.
