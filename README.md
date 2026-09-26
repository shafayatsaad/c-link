# C-Link production package

C-Link 0.5.0 is an OpenAI-compatible chat and durable-memory gateway. It provides local single-user mode and an operator-provisioned, API-key authenticated tenant mode for one self-hosted instance.

## Quick start on Windows

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

## Production deployment

The supported internet-facing template is a single Linux Docker host with Docker Compose, C-Link, SQLite, and Caddy for TLS. It requires an operator-managed domain, firewall access on ports 80/443, and a trusted model endpoint. See [the deployment guide](docs/DEPLOYMENT.md) before using it with real users.

Provision tenants on the host with `c-link tenant create "Name"`. It prints the initial high-entropy API key once; only the key hash is persisted. `/v1/*` requests require the key. Each tenant has isolated sessions and context. There is no public signup, account portal, or self-service key management.

## Benchmarks

See the [project README](../README.md#benchmark-evidence) for a concise results table and caveats. Raw benchmark reports and scripts are under `benchmark-results/` and `scripts/`. These are one-host smoke measurements, not an SLA or capacity claim.

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
