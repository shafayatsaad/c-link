# Connect apps to C-Link

C-Link exposes an OpenAI-compatible Chat Completions API. Configure clients with an API base URL ending in `/v1`, then use `/chat/completions` or `/models` through the client SDK.

## Start locally

Start your llama.cpp-compatible model server first. If it listens on port `9931`, start C-Link on `9940` in another PowerShell window:

```powershell
Set-Location .\production
$env:C_LINK_LLAMACPP_URL = "http://127.0.0.1:9931"
& .\.venv\Scripts\c-link.exe doctor --require-model
& .\.venv\Scripts\c-link.exe run --host 127.0.0.1 --port 9940
```

In a second terminal, verify readiness:

```powershell
Invoke-RestMethod http://127.0.0.1:9940/health
Invoke-RestMethod http://127.0.0.1:9940/health/provider
Invoke-RestMethod http://127.0.0.1:9940/v1/models
```

The current local setup uses model provider port `9931` and C-Link port `9940`. C-Link's `local` auth mode is intended for loopback use. To expose a local process on another interface, use an API key and a trusted network boundary; for internet-facing deployment, use the [deployment guide](DEPLOYMENT.md), which requires API-key auth behind TLS.

## API endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | C-Link and SQLite readiness; does not call the model provider |
| `GET /health/provider` | Confirm that the model provider is reachable |
| `GET /docs` | Interactive local Swagger documentation |
| `GET /openapi.json` | Local OpenAPI schema |
| `GET /v1/models` | Return the configured public model alias |
| `POST /v1/chat/completions` | OpenAI-compatible JSON response or SSE stream |
| `POST /v1/sessions` | Create a durable session |
| `GET /v1/sessions` | List sessions for the current tenant |
| `GET /v1/sessions/{id}/messages` | Paginated session history |
| `DELETE /v1/sessions/{id}` | Delete a session owned by the current tenant |
| `GET` / `PUT /v1/sessions/{id}/context` | Read or replace canonical context |
| `GET /v1/sessions/{id}/snapshots` | List context snapshots |
| `POST /v1/sessions/{id}/context/restore` | Restore a snapshot as a new context version |
| `GET /v1/sessions/{id}/events` | Read context and archive event history |
| `POST /v1/messages` | Archive a message in a session |
| `POST /v1/context/compile` | Compile a bounded context packet |
| `POST /v1/memory/query` | Query context and optional historical evidence |

Chat requests support `memory_mode: "auto"`, `"current"`, `"why"`, and `"historical"`. `current` skips archive retrieval. C-Link reports the selected mode, evidence count, and bounded packet information under `c_link_memory`.

## Stable conversation sessions

An app should send the same stable session ID on every turn that belongs in one C-Link archive. Set the `X-C-Link-Session-Id` header or the `session_id` JSON field. When neither is provided, C-Link creates an ID for that request, so the app cannot reliably continue the same stored session on a later call.

C-Link bounds client-supplied history before forwarding it to the model. By default it keeps system/developer instructions and a configured window of recent conversation messages (`C_LINK_MAX_RECENT_MESSAGES`, default 12; `C_LINK_MAX_RECENT_CHARS`, default 8,000). The full durable history remains in SQLite.

Example in PowerShell:

```powershell
$base = "http://127.0.0.1:9940"
$session = "work-notes-01"
Invoke-RestMethod -Method Post -Uri "$base/v1/sessions" `
  -ContentType "application/json" `
  -Body (@{ session_id = $session; project_id = "work" } | ConvertTo-Json)

$body = @{
  model = "local-model"
  session_id = $session
  messages = @(@{ role = "user"; content = "Remember this project uses Python 3.13." })
} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Post -Uri "$base/v1/chat/completions" `
  -ContentType "application/json" -Body $body
```

## Open WebUI

In **Settings → Admin → Connections → OpenAI API**, add:

- URL: `http://127.0.0.1:9940/v1` for local use, or your HTTPS deployment URL ending in `/v1`.
- API key: blank in local mode; for a multi-user deployment, use that user's tenant key.
- Model: `local-model`.

If Open WebUI runs in Docker, the host address is commonly `http://host.docker.internal:9940/v1`. The C-Link process must be reachable from the container. Prefer a private container network; do not disable authentication or expose an unauthenticated server to an untrusted network. Open WebUI's provider connection setup is documented in the [Open WebUI guide](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/).

For durable per-conversation memory, configure the Open WebUI connector or an intermediary to pass a stable `X-C-Link-Session-Id`. Do not place a privileged C-Link key in browser JavaScript.

## Continue for VS Code

Add an OpenAI-compatible model to Continue's `config.yaml`:

```yaml
name: C-Link local
version: 1.0.0
schema: v1
models:
  - name: Qwen through C-Link
    provider: openai
    apiBase: http://127.0.0.1:9940/v1
    apiKey: local-only
    model: local-model
    roles:
      - chat
      - edit
```

For a deployed instance, use its HTTPS API base, and inject the tenant key through the app's supported secret storage. Continue's OpenAI-compatible endpoint fields are described in its [configuration reference](https://docs.continue.dev/reference). C-Link does not implement embeddings or repository indexing; use separate providers for those capabilities.

## Hermes Agent

Configure an OpenAI-compatible custom provider in Hermes:

```yaml
providers:
  c-link:
    api: http://127.0.0.1:9940/v1
    transport: chat_completions
    key_env: C_LINK_API_KEY
    session_affinity_header: X-C-Link-Session-Id
    models:
      local-model:
        context_length: 8192
```

For local mode, omit `key_env`. In a multi-user deployment, set `C_LINK_API_KEY` to the key issued for that tenant using Hermes' secret mechanism. Hermes documents custom providers in its [provider guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md) and [model configuration guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuring-models.md).

## OpenAI SDK and compatible clients

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9940/v1",
    api_key="local-only",  # For a deployed instance, load that tenant's key from a secret store.
)
answer = client.chat.completions.create(
    model="local-model",
    messages=[{"role": "user", "content": "Hello from C-Link."}],
    extra_body={"session_id": "my-conversation-01", "memory_mode": "auto"},
    stream=True,
)
for chunk in answer:
    text = chunk.choices[0].delta.content
    if text:
        print(text, end="", flush=True)
```

C-Link implements the Chat Completions route, model listing, JSON replies, and SSE streaming. It is not an OpenAI Responses API, Anthropic Messages API, or embeddings endpoint. Tool calls may pass through as model output, but C-Link does not execute tools. Apps requiring those APIs need a compatible provider or adapter.

## Multi-user authentication and network access

The included internet-facing setup requires `C_LINK_ENV=production`, `C_LINK_AUTH_MODE=api_key`, and `C_LINK_BEHIND_TLS_PROXY=true`. Every `/v1/*` request requires `Authorization: Bearer <key>`. The operator creates tenants and keys with the host CLI; C-Link does not expose public signup or tenant administration endpoints. Different tenants have separate sessions, archives, context, and snapshots. Multiple keys issued to one tenant share that tenant's data.

Keep C-Link on `127.0.0.1` for a single local user. Production traffic should go through a TLS-terminating reverse proxy. Browser clients need exact trusted CORS origins in `C_LINK_CORS_ORIGINS`; wildcard CORS is rejected. Server-to-server apps generally do not require CORS.

## Backup and restore

For a local PowerShell install, stop C-Link before restore:

```powershell
& .\.venv\Scripts\c-link.exe backup --output .\backups\c-link.sqlite3
& .\.venv\Scripts\c-link.exe restore .\backups\c-link.sqlite3
```

The restore command validates the backup, atomically replaces the configured database, and saves the previous database as a `.pre-restore-<timestamp>` copy when possible. For Docker operations and off-host backup guidance, see [Deployment](DEPLOYMENT.md).
