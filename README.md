# C-Link production package

C-Link is a local-first, OpenAI-compatible chat and durable-memory gateway. It runs on your own computer, uses SQLite for local conversation storage, and does not require a C-Link account or sign-up. Chat generation requires a separately installed OpenAI-compatible model server; memory and session APIs can be used without one.

## Quick start on Windows

Install Python 3.13 and an OpenAI-compatible local model server such as llama.cpp. Start the model server on port `9931`, then open PowerShell:

```powershell
Set-Location D:\Coding\c-link\production
py -3.13 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
$env:C_LINK_LLAMACPP_URL = "http://127.0.0.1:9931"
& .\.venv\Scripts\c-link.exe doctor --require-model
& .\.venv\Scripts\c-link.exe run --host 127.0.0.1 --port 9940
```

Keep this terminal open while using C-Link. If port `9940` is already in use, keep the existing server or choose another free port and update the app's API base URL.

Check service health from another PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:9940/health
Invoke-RestMethod http://127.0.0.1:9940/health/provider
Invoke-RestMethod http://127.0.0.1:9940/v1/models
```

For API setup, supported routes, integrations, backups, and session IDs, see [the root README](../README.md) and [the integrations guide](docs/INTEGRATIONS.md).

## Development checks

```powershell
& .\.venv\Scripts\python.exe -m pytest
& .\.venv\Scripts\python.exe -m compileall -q src tests
```

The app binds to loopback by default. Conversation data and configuration stay in the local `production/data/` directory unless you change the configured paths. Back up that data directory using the CLI backup command before moving or replacing the database.
