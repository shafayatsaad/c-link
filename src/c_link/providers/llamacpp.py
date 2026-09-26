"""Small OpenAI-compatible adapter for a local llama.cpp server."""

import json
import os
from pathlib import Path
from collections.abc import Iterator
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    """The configured model provider could not complete a request."""


def provider_api_key() -> str | None:
    """Load the provider credential directly or from a mounted secret file."""
    key = os.environ.get("C_LINK_LLAMACPP_API_KEY")
    if key:
        return key
    secret_file = os.environ.get("C_LINK_LLAMACPP_API_KEY_FILE")
    if secret_file:
        try:
            value = Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderError("could not read configured provider credential file") from exc
        return value or None
    return None


def provider_base_url() -> str:
    return os.environ.get("C_LINK_LLAMACPP_URL", "http://127.0.0.1:9932").rstrip("/")


def chat_completions_url() -> str:
    base = provider_base_url()
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def complete_chat(payload: dict) -> dict:
    """Forward a non-streaming chat completion to llama-server."""
    request_body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = provider_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(chat_completions_url(), data=request_body, headers=headers, method="POST")
    timeout = float(os.environ.get("C_LINK_PROVIDER_TIMEOUT_SECONDS", "120"))
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ProviderError(f"llama.cpp returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"could not reach llama.cpp at {chat_completions_url()}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("llama.cpp returned an invalid JSON response") from exc
    if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
        raise ProviderError("llama.cpp response did not contain chat completion choices")
    return result


def list_models() -> dict:
    """Return the provider's OpenAI-compatible model list."""
    base = provider_base_url()
    models_url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
    headers = {"Accept": "application/json"}
    api_key = provider_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(models_url, headers=headers)
    timeout = float(os.environ.get("C_LINK_PROVIDER_TIMEOUT_SECONDS", "120"))
    try:
        with closing(urlopen(request, timeout=timeout)) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ProviderError(f"llama.cpp returned HTTP {exc.code} while listing models") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"could not reach llama.cpp at {provider_base_url()}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("llama.cpp returned an invalid model list") from exc
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise ProviderError("llama.cpp model list did not contain a data array")
    return result


def tokenize_text(content: str) -> int:
    """Count tokens in text using the configured llama.cpp tokenizer."""
    base = provider_base_url()
    if base.endswith("/v1"):
        base = base[:-3]
    data = json.dumps({"content": content, "add_special": False, "parse_special": True}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = provider_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(base + "/tokenize", data=data, headers=headers, method="POST")
    timeout = float(os.environ.get("C_LINK_PROVIDER_TIMEOUT_SECONDS", "120"))
    try:
        with closing(urlopen(request, timeout=timeout)) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ProviderError(f"llama.cpp returned HTTP {exc.code} while tokenizing context") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"could not reach llama.cpp tokenizer at {base}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("llama.cpp tokenizer returned invalid JSON") from exc
    tokens = result.get("tokens") if isinstance(result, dict) else None
    if not isinstance(tokens, list):
        raise ProviderError("llama.cpp tokenizer response did not contain a tokens array")
    return len(tokens)


def open_chat_stream(payload: dict):
    """Open an upstream SSE response, raising before the downstream response starts."""
    body = dict(payload)
    body["stream"] = True
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    api_key = provider_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(chat_completions_url(), data=data, headers=headers, method="POST")
    timeout = float(os.environ.get("C_LINK_PROVIDER_TIMEOUT_SECONDS", "120"))
    try:
        return urlopen(request, timeout=timeout)
    except HTTPError as exc:
        # Avoid leaking provider response bodies, which may contain local paths.
        raise ProviderError(f"llama.cpp returned HTTP {exc.code} for streaming chat") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"could not reach llama.cpp at {chat_completions_url()}: {exc}") from exc


def iter_sse_data(response) -> Iterator[str]:
    """Yield each SSE event's joined data field from an HTTP response."""
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            data_lines.append(value[1:] if separator and value.startswith(" ") else value)
    if data_lines:
        yield "\n".join(data_lines)
