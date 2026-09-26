# Connecting apps to C-Link

C-Link exposes a local OpenAI Chat Completions API. The API base URL ends in `/v1`; clients should send chat calls to `/chat/completions` and model discovery to `/models`.

## Start C-Link

The default port layout is:

| Service | Default URL |
| --- | --- |
| C-Link gateway | `http://127.0.0.1:9931` |
| llama.cpp model server | `http://127.0.0.1:9932` |
| OpenAI-compatible API base | `http://127.0.0.1:9931/v1` |

Your current Qwen server already uses port `9931`, so use `9940` for C-Link unless you move llama.cpp to `9932`:

```powershell
Set-Location D:\Coding\c-link\production
& .\scripts\start.ps1 -Port 9940 -ModelUrl http://127.0.0.1:9931
```

Check the gateway and model provider in a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:9940/health
Invoke-RestMethod http://127.0.0.1:9940/health/provider
Invoke-RestMethod http://127.0.0.1:9940/v1/models
```

The current workspace’s gateway is already set up to use Qwen at `9931` and C-Link at `9940`. For repeatable local settings, copy `config/local.example.ps1` to `config/local.ps1`, edit it, then start with `& .\scripts\start.ps1 -Port 9940`. The private `local.ps1` is Git-ignored.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | C-Link process and SQLite readiness; does not contact the model server |
| `GET /health/provider` | Verify llama.cpp is reachable and reports at least zero or more models |
| `GET /docs` | Interactive Swagger API documentation |
| `GET /openapi.json` | OpenAPI schema |
| `GET /v1/models` | Proxy the configured model server’s model list |
| `POST /v1/chat/completions` | OpenAI-compatible chat; `stream: true` uses SSE |
| `POST /v1/sessions` | Create a durable C-Link session |
| `POST /v1/messages` | Archive an app message in a session |
| `GET/PUT /v1/sessions/{id}/context` | Read or replace canonical context |
| `GET /v1/sessions/{id}/snapshots` | List context snapshots |
| `POST /v1/sessions/{id}/context/restore` | Restore a snapshot as a new context version |
| `GET /v1/sessions/{id}/events` | Read context and archive event history |
| `POST /v1/context/compile` | Compile a bounded context packet |
| `POST /v1/memory/query` | Retrieve current context and optionally historical evidence |

For context selection, the chat request accepts `memory_mode: "auto"`, `"current"`, `"why"`, or `"historical"`. `auto` routes from the question; `current` explicitly avoids archive retrieval. C-Link reports the selected mode, evidence count, and bounded packet size in `c_link_memory`.

## Keep a stable memory session

An app must send the same stable session ID on every turn if it wants one durable C-Link archive. Set either the `X-C-Link-Session-Id` HTTP header or the `session_id` field in the JSON request body. If neither is present, C-Link creates a new session for each request.

C-Link bounds client-supplied chat history before forwarding it to the model: it keeps system/developer instructions and a configured window of recent conversation messages (`C_LINK_MAX_RECENT_MESSAGES`, default 12; `C_LINK_MAX_RECENT_CHARS`, default 8,000). The full durable history remains in SQLite. If an app does not pass a stable ID, its own recent chat may still be sent on each call, but C-Link cannot join those requests into one archive session.

Example using a persistent session in PowerShell:

```powershell
$base = "http://127.0.0.1:9940"
$session = "work-notes-01"
Invoke-RestMethod -Method Post -Uri "$base/v1/sessions" `
  -ContentType "application/json" `
  -Body (@{ session_id = $session; project_id = "work" } | ConvertTo-Json)

$body = @{
  model = (Invoke-RestMethod "$base/v1/models").data[0].id
  session_id = $session
  messages = @(@{ role = "user"; content = "Remember this project uses Python 3.13." })
} | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Post -Uri "$base/v1/chat/completions" `
  -ContentType "application/json" -Body $body
```

## Open WebUI

In **Settings → Admin → Connections → OpenAI API**, add a connection:

- URL: `http://127.0.0.1:9940/v1`
- API key: leave blank if `C_LINK_API_KEY` is unset; otherwise enter that key
- Model: select the model returned by C-Link’s `/v1/models`

If Open WebUI runs in Docker, use `http://host.docker.internal:9940/v1`. The C-Link process must then bind to a non-loopback interface, which requires `C_LINK_API_KEY`; restrict Windows Firewall access to the intended host. Open WebUI’s provider connection uses its server-side OpenAI-compatible connection flow. Its current setup steps are documented in the [Open WebUI guide](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/).

For durable per-conversation C-Link memory, configure the Open WebUI connector or an intermediary to pass a stable `X-C-Link-Session-Id` header. Without that custom session ID, C-Link creates separate sessions per call. Do not put a privileged C-Link key into browser JavaScript.

## Continue for VS Code

Add a chat model to Continue’s `config.yaml` (the exact model ID is available from `/v1/models`):

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

If C-Link authentication is enabled, replace `local-only` with the configured key. Continue’s OpenAI-compatible custom endpoint fields and roles are described in its [configuration reference](https://docs.continue.dev/reference). C-Link does not implement embeddings or repository indexing; keep those roles configured to the separate provider Continue uses for those capabilities.

## Hermes Agent

In Hermes’ `config.yaml`, define a custom OpenAI-compatible provider:

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

When no C-Link API key is set, omit `key_env`. Select the provider/model with Hermes’ model configuration command. Hermes documents custom provider `api`, `transport`, model, key, and session-affinity fields in its [provider guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md) and [model configuration guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/configuring-models.md).

## OpenAI SDK and other compatible clients

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9940/v1",
    api_key="local-only",  # Use C_LINK_API_KEY here if authentication is enabled.
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

The API supports the Chat Completions route, model listing, JSON/non-streaming replies, and SSE streaming. It is not an OpenAI Responses API, Anthropic Messages API, or embeddings endpoint. Model tool calls may pass through, but C-Link does not execute tools. Apps requiring those additional APIs need their own compatible provider or an adapter.

## Network access and security

Keep C-Link on `127.0.0.1` for one local user. When you need a container or other device to connect, set `C_LINK_API_KEY` to a long random secret and bind C-Link to the required interface. The CLI rejects a non-loopback bind without a key. Requests to `/v1/*` require `Authorization: Bearer <key>` when configured.

Browser-origin access is denied unless `C_LINK_CORS_ORIGINS` lists exact trusted origins (comma-separated); `*` is rejected. CORS is unnecessary when an app’s backend talks to C-Link. C-Link currently has a single shared API key, not per-user identity or multi-tenant authorization, and it does not terminate TLS. Do not expose it to an untrusted LAN or the public internet without a secured reverse proxy and access controls.

## Backup and restore

```powershell
& .\.venv\Scripts\c-link.exe backup --output .\backups\c-link.sqlite3
& .\.venv\Scripts\c-link.exe restore .\backups\c-link.sqlite3
```

Restore only after stopping C-Link. The restore command validates the source database, atomically replaces the configured database, and saves the previous database as a `.pre-restore-<timestamp>` safety copy.

## Current product boundary

C-Link is a local, single-user context/memory and chat gateway. It does not yet provide automatic context extraction, embeddings, repository indexing, agent tool execution, MCP, multi-user authorization, installer/auto-update, or deployment telemetry. These should not be inferred from its OpenAI-compatible chat route.
