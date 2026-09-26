# Connect apps to C-Link

C-Link exposes an OpenAI-compatible Chat Completions API. Configure clients with an API base URL ending in `/v1`, then use `/chat/completions` or `/models` through the client SDK.

## Start locally

Start your llama.cpp-compatible model server first. If it listens on port `9931`, start C-Link on `9940` in another PowerShell window:

```powershell
Set-Location .
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

The example uses model provider port `9931` and C-Link port `9940`. C-Link's default `local` auth mode is intended for loopback use on your own computer. Leave it bound to `127.0.0.1` for normal individual use.

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

- URL: `http://127.0.0.1:9940/v1`.
- API key: blank in local mode.
- Model: `local-model`.

If Open WebUI runs in Docker, its container may not be able to reach a host service bound to loopback. For a simple local setup, run both apps on the same host outside separate containers. Open WebUI's provider connection setup is documented in the [Open WebUI guide](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/).

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

Continue's OpenAI-compatible endpoint fields are described in its [configuration reference](https://docs.continue.dev/reference). C-Link does not implement embeddings or repository indexing; use separate providers for those capabilities.

## Hermes Agent

Run `hermes model`, choose **Custom endpoint**, and enter the C-Link API base URL `http://127.0.0.1:9940/v1`, model name `local-model`, and the API key if your local C-Link configuration requires one. Hermes documents this setup in its [custom endpoint guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md#general-setup) and [model configuration guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuring-models.md).

Hermes also supports a `session_affinity_header` option for named providers. Configure that option as `X-C-Link-Session-Id` only if your Hermes setup uses a named provider and should send stable conversation IDs to C-Link.

## OpenAI SDK and compatible clients

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9940/v1",
    api_key="local-only",  # Local mode does not validate an API key.
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

## Optional shared use on a trusted local network

The default install is for one person on one computer and listens on `127.0.0.1`. An administrator can optionally enable API-key authentication and create local tenants with the `c-link tenant` CLI for several trusted users sharing a machine or private network. Each tenant has its own sessions and archive. C-Link has no public registration or account service. Keep shared use on a trusted private network and configure authentication before allowing other devices to connect.

## Backup and restore

For a local PowerShell install, stop C-Link before restore:

```powershell
& .\.venv\Scripts\c-link.exe backup --output .\backups\c-link.sqlite3
& .\.venv\Scripts\c-link.exe restore .\backups\c-link.sqlite3
```

The restore command validates the backup, atomically replaces the configured database, and saves the previous database as a `.pre-restore-<timestamp>` copy when possible.
