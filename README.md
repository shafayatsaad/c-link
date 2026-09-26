# C-Link production package

C-Link 0.5.0 is an open-source-intended, local-first OpenAI-compatible chat and durable-memory gateway. The normal installation runs on loopback for one person's apps and needs no C-Link account or sign-up. Optional API-key tenants are available when a machine operator intentionally shares one instance.

## Quick start on Windows

Run from the repository root:

Start an OpenAI-compatible model server, then run in PowerShell:

```powershell
Set-Location .\production
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:C_LINK_LLAMACPP_URL = "http://127.0.0.1:9931"
& .\.venv\Scripts\c-link.exe doctor --require-model
& .\.venv\Scripts\c-link.exe run --host 127.0.0.1 --port 9940
```

Keep the API bound to loopback for local use. If port 9940 is occupied, choose a free port and use it consistently in the app's API base URL. Do not terminate the model server to resolve a C-Link port conflict.

## Optional remote deployment

Remote hosting is not required to install or use C-Link. For an operator who intentionally wants a shared remote endpoint, the repository includes a single Linux Docker host template with Docker Compose, SQLite, and Caddy TLS. It requires an operator-managed domain, firewall access on ports 80/443, and a trusted model endpoint. See [the optional deployment guide](docs/DEPLOYMENT.md).

Provision tenants on the host with `c-link tenant create "Name"`. It prints the initial high-entropy API key once; only the key hash is persisted. `/v1/*` requests require the key. Each tenant has isolated sessions and context. There is no public signup, account portal, or self-service key management.

## Benchmarks

See the [project README](../README.md#benchmark-snapshot) for the visual results summary, table, and caveats. Raw benchmark reports and scripts are under `benchmark-results/` and `scripts/`. These are one-host smoke measurements, not an SLA or capacity claim.

## Development and verification

```powershell
& .\.venv\Scripts\python.exe -m pytest
& .\.venv\Scripts\python.exe -m compileall -q src tests scripts
docker compose --env-file deploy/.env.example -f deploy/compose.yaml config --quiet
```

The Compose config command validates interpolation and syntax only. Building and running the container, acquiring a certificate, and testing the public deployment require a running Docker host, real DNS, and open firewall ports.

## Documentation

- [App integrations](docs/INTEGRATIONS.md)
- [Self-hosted deployment](docs/DEPLOYMENT.md)
- Project architecture and working context are kept outside this production package.

Conversation data is stored in SQLite. Protect the database volume and backups with host-level access controls and encryption. The package does not currently provide database encryption, horizontal scaling, persistent distributed rate limiting, user registration, or application-level monitoring.
