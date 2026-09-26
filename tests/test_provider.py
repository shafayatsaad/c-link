from io import BytesIO
import json

from c_link.providers.llamacpp import (
    chat_completions_url,
    iter_sse_data,
    provider_api_key,
    provider_base_url,
    tokenize_text,
)


def test_provider_key_can_be_read_from_secret_file(tmp_path, monkeypatch):
    secret = tmp_path / "provider.key"
    secret.write_text("private-provider-token\n", encoding="utf-8")
    monkeypatch.delenv("C_LINK_LLAMACPP_API_KEY", raising=False)
    monkeypatch.setenv("C_LINK_LLAMACPP_API_KEY_FILE", str(secret))
    assert provider_api_key() == "private-provider-token"


def test_llamacpp_adapter_normalizes_base_url(monkeypatch):
    monkeypatch.setenv("C_LINK_LLAMACPP_URL", "http://127.0.0.1:9932/")
    assert provider_base_url() == "http://127.0.0.1:9932"
    assert chat_completions_url() == "http://127.0.0.1:9932/v1/chat/completions"

    monkeypatch.setenv("C_LINK_LLAMACPP_URL", "http://model.local:8080/v1")
    assert chat_completions_url() == "http://model.local:8080/v1/chat/completions"


def test_sse_parser_yields_data_events_and_skips_comments():
    stream = BytesIO(
        b": keep-alive\r\n\r\ndata: {\"delta\":\r\ndata: \"hello\"}\r\n\r\ndata: [DONE]\r\n\r\n"
    )
    assert list(iter_sse_data(stream)) == ['{"delta":\n"hello"}', "[DONE]"]


def test_tokenizer_uses_server_tokenize_route_and_bearer_key(monkeypatch):
    monkeypatch.setenv("C_LINK_LLAMACPP_URL", "http://127.0.0.1:9932/v1")
    monkeypatch.setenv("C_LINK_LLAMACPP_API_KEY", "provider-key")

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://127.0.0.1:9932/tokenize"
        assert request.get_header("Authorization") == "Bearer provider-key"
        assert json.loads(request.data)["content"] == "context"
        return BytesIO(b'{"tokens":[1,2,3]}')

    monkeypatch.setattr("c_link.providers.llamacpp.urlopen", fake_urlopen)
    assert tokenize_text("context") == 3
