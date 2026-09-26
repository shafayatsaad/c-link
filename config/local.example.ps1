# Optional local C-Link settings for Windows PowerShell.
# Copy this file to config/local.ps1 and edit it; local.ps1 is ignored by Git.

$env:C_LINK_LLAMACPP_URL = "http://127.0.0.1:9932"
$env:C_LINK_MODEL_NAME = "local-model"
# Optional if the upstream model endpoint requires a different model ID.
# $env:C_LINK_UPSTREAM_MODEL_NAME = "upstream-model-id"
$env:C_LINK_DATA_DIR = "data"
$env:C_LINK_MAX_RECENT_MESSAGES = "12"
$env:C_LINK_MAX_RECENT_CHARS = "8000"
$env:C_LINK_PROVIDER_TIMEOUT_SECONDS = "120"
# Optional: query llama.cpp's tokenizer to include exact context packet token counts.
# $env:C_LINK_COUNT_CONTEXT_TOKENS = "true"

# Keep C-Link bound to loopback for a single-user machine.
# If another device/container must connect, set a strong key and bind explicitly:
# $env:C_LINK_API_KEY = (python -c "import secrets; print(secrets.token_urlsafe(32))")
# $env:C_LINK_CORS_ORIGINS = "http://localhost:3000"
