# C-Link

C-Link is a local, single-user context and memory gateway with an OpenAI-compatible Chat Completions API. The archive, context, CLI, backup, and benchmark features work without a model; chat requests can be forwarded to a local llama.cpp server.

## Current capabilities

- SQLite sessions, immutable archived messages, context events, and versioned snapshots
- Active structured context with decision supersession
- Atomic `context.md` projection at `data/contexts/<session-id>/context.md`
- SQLite FTS5 message search, with a deterministic `LIKE` fallback when FTS5 is unavailable
- Current/why/historical memory query routing and bounded evidence excerpts
- FastAPI and a local CLI
- OpenAI-compatible `/v1/chat/completions`, `/v1/models`, and streamed SSE responses backed by llama.cpp
- bounded recent conversation forwarding with stable session IDs in the body or `X-C-Link-Session-Id`
- optional bearer-token protection for `/v1/*`; non-loopback binds require a key
- explicit CORS origin allowlist, provider health check, and CLI doctor
- verified SQLite backups, safe restore with pre-restore copy, and context snapshot restore

The API is bound to `127.0.0.1:9931` by default. Set `C_LINK_DATA_DIR` to move the data directory, or set `C_LINK_DATABASE` to choose the SQLite file directly. The model adapter defaults to `http://127.0.0.1:9932`; override it with `C_LINK_LLAMACPP_URL` if llama-server uses another address. If llama-server currently occupies 9931, start C-Link on 9940:

```powershell
Set-Location D:\Coding\c-link\production
& .\scripts\start.ps1 -Port 9940 -ModelUrl http://127.0.0.1:9931
```

To check that both services are ready, run these commands in another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:9940/health
Invoke-RestMethod http://127.0.0.1:9940/health/provider
Invoke-RestMethod http://127.0.0.1:9940/v1/models
```

## Run

```powershell
py -m pip install -e .
c-link run
```

On Windows, `scripts/start.ps1` loads the optional ignored `config/local.ps1`, detects an occupied port, and starts the gateway. App connection examples and session-affinity instructions are in [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

Create a session:

```powershell
c-link session create --project-id demo
```

The API exposes `GET /health`, `GET /health/provider`, `GET /v1/models`, `POST /v1/sessions`, `POST /v1/messages`, `POST /v1/context/items`, `GET` and `PUT /v1/sessions/{id}/context`, context snapshot listing/restore, `GET /v1/sessions/{id}/events`, `POST /v1/context/compile`, `POST /v1/memory/query`, and streaming or non-streaming `POST /v1/chat/completions`.

The chat endpoint preserves the OpenAI Chat Completions request/response shape and adds `session_id` and `c_link_memory` metadata. C-Link forwards a bounded recent window of client chat history and archives the turn. Use a stable `session_id` or `X-C-Link-Session-Id` across calls for one durable memory session.

## Benchmark layers

The memory core can be benchmarked without Qwen or llama.cpp:

1. **Storage/retrieval:** write volume, query latency, recall on a fixed archive, and FTS5 versus fallback behavior.
2. **Context behavior:** supersession correctness, evidence-window bounds, context portability, and active packet size over long sessions.
3. **Gateway path:** local HTTP write/query latency and throughput. `scripts/benchmark_api.py` measures this against a running C-Link service and creates a persistent benchmark session.
4. **Model path:** answer quality, prompt processing, generation tokens/second, VRAM/RAM, and end-to-end latency. The llama.cpp adapter is implemented; measuring this layer requires a running llama-server with the selected model.

Until llama-server is running, C-Link is runnable and memory benchmarks are meaningful, but claims about model quality or inference speed are not.

### Run the repeatable memory-core benchmark

From this directory, with the package installed:

```powershell
python scripts/benchmark_memory.py --sizes 500,2000,10000 --repeats 50 --warmups 5 --output benchmark-results/memory-baseline.json
```

This uses an isolated temporary database, checks fixed-query archive recall, records retrieval p50/p95, tracks database size, and checks that active context and compiled packets stay bounded as archived messages grow. It does not alter `data/c_link.db`.

### Run the HTTP benchmark

With C-Link running in another terminal:

```powershell
python scripts/benchmark_api.py --base-url http://127.0.0.1:9931 --messages 500 --queries 50 --warmups 5 --output benchmark-results/http-baseline.json
```

This creates a new session in the running service's database and leaves it there, so each run adds benchmark messages. Use a disposable database when comparing repeated runs.

The completed in-process baseline on Python 3.13.11 / SQLite 3.50.4 / Windows 11 retrieved the fixed evidence at 500, 2,000, and 10,000 messages. At 10,000 messages, measured retrieval was 2.738 ms p50 / 2.980 ms p95; the compiled packet was 1,103 characters and the SQLite database was 2,756,608 bytes. This is a local memory-core benchmark, not model latency.

The current full report is saved at `benchmark-results/memory-baseline.json`. A temporary isolated HTTP smoke run is saved at `benchmark-results/http-baseline.json`; it reached 52.16 ingested messages/s and 24.05 ms query p50 / 31.67 ms p95 over 20 timed queries. HTTP results include loopback transport and Python client overhead and should be rerun on the target machine before setting performance gates.

### Latest local rerun (2026-09-26)

Latest reports from this machine are saved as `benchmark-results/memory-release.json`, `benchmark-results/http-release.json`, and `benchmark-results/model-release.json`. The HTTP and model runs used a disposable C-Link database under `benchmark-results/release-data`; they did not add benchmark rows to the normal `data` database.

- **Memory core:** fixed evidence was recalled at 500, 2,000, and 10,000 messages. At 10,000 messages, query p50/p95 was 3.817/5.282 ms, the packet was 1,103 characters, and the database was 2,756,608 bytes.
- **HTTP gateway:** 500 messages ingested at 53.79 messages/s; write p50/p95 was 20.179/32.461 ms and query p50/p95 was 14.690/29.861 ms. The seeded evidence was recalled.
- **Qwen model path:** Qwen2.5-Coder-7B-Instruct Q5_K_M answered both provisional math cases correctly on manual review in raw and C-Link runs. Generation speed was about 55 tokens/s. The corrected archive-recall run returned one evidence item through C-Link and zero for current-context-only; end-to-end latency was 598 ms with retrieval, 1,232 ms current-only, and 1,878 ms raw. These are single-run smoke results, not a quality claim.
- **Model TTFT:** raw math was 54–74 ms, C-Link math 107–120 ms; archive recall was 54 ms raw, 407 ms current-only, and 138 ms with retrieval.
- **Resource sample:** a separate periodic sample measured llama-server working set at 4.68 GiB and GPU memory at 5,555 MiB of 6,144 MiB on the RTX 3060 Laptop GPU. Sampling can miss instantaneous peaks.

The model comparison uses a 512-token output cap and measures time to first streamed text (TTFT). To include exact prompt packet token counts in the report, set `$env:C_LINK_COUNT_CONTEXT_TOKENS = "true"` before starting C-Link; this asks llama.cpp to tokenize the packet on each request. The two math prompts are generated provisional samples because the exact project-approved questions are absent from available context. Review stored answers manually and replace the cases before treating them as a fixed quality regression test. Each condition has one run, so these numbers are diagnostic rather than performance guarantees.

### Run the model comparison

Start llama-server and C-Link, then run (adjust URLs and model ID for your setup):

```powershell
python scripts/benchmark_model.py --model-base-url http://127.0.0.1:9932 --c-link-base-url http://127.0.0.1:9931 --model local-model
```

This compares raw-model math answers with C-Link current-context answers, then compares raw archive recall, C-Link current-only, and C-Link forced retrieval. The report stores answers and expected values for manual grading and measures TTFT for streamed calls. Set `--max-tokens` when a workload needs a different completion limit; the default is 512. Record RAM/VRAM during the run with Windows Task Manager or `nvidia-smi`.

## Current boundary

The current build is intended for a local single-user gateway. It does not include automatic extraction of context items from free-form messages, embeddings, repository indexing, MCP, agent tool execution, per-user authorization, TLS termination, installer/auto-update, or deployment certification. See [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md) for exact compatibility boundaries and safe local use.
