import json
from io import BytesIO
import sqlite3

import pytest
from fastapi.testclient import TestClient

from c_link.api.app import app, get_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("C_LINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("C_LINK_DATABASE", str(tmp_path / "c_link.sqlite"))
    monkeypatch.setenv("C_LINK_LLAMACPP_URL", "http://127.0.0.1:9932")
    get_store.cache_clear()
    with TestClient(app, client=("127.0.0.1", 12345)) as test_client:
        yield test_client
    get_store.cache_clear()


def create_session(client, project_id="project-a"):
    response = client.post("/v1/sessions", json={"project_id": project_id})
    assert response.status_code == 201
    return response.json()["session_id"]


def test_health_initializes_provider_independent_service(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok", "service": "c-link", "memory": "ready",
        "provider": "llama.cpp", "provider_url": "http://127.0.0.1:9932",
        "provider_check": "not_performed",
    }


def test_local_auth_mode_rejects_non_loopback_api_clients(client):
    from c_link.api.app import app

    with TestClient(app, client=("203.0.113.9", 12345)) as remote_client:
        assert remote_client.get("/v1/models").status_code == 403


def test_oversized_http_request_is_rejected_before_endpoint_dispatch(client):
    response = client.get("/v1/models", headers={"Content-Length": "2097153"})
    assert response.status_code == 413


def test_message_archive_and_historical_query_returns_evidence(client):
    session_id = create_session(client)
    text = "Why did we choose SQLite for the initial archive? It is embedded and needs no server."
    archived = client.post("/v1/messages", json={
        "session_id": session_id, "role": "user", "content": text
    })
    assert archived.status_code == 201

    response = client.post("/v1/memory/query", json={
        "session_id": session_id,
        "query": "Why did we choose SQLite for the initial archive?",
    })
    body = response.json()
    assert response.status_code == 200
    assert body["mode"] == "why"
    assert body["retrieval_performed"] is True
    assert body["evidence"][0]["excerpt"] == text
    assert text in body["context_packet"]


def test_current_query_uses_canonical_context_without_archive_search(client):
    session_id = create_session(client)
    response = client.put(f"/v1/sessions/{session_id}/context", json={
        "reason": "objective_set",
        "context": {
            "session_id": session_id,
            "current_objective": "Build a reliable local memory gateway.",
        },
    })
    assert response.status_code == 200

    result = client.post("/v1/memory/query", json={
        "session_id": session_id, "query": "What is the current objective?"
    }).json()
    assert result["mode"] == "current"
    assert result["retrieval_performed"] is False
    assert "Build a reliable local memory gateway." in result["context_packet"]


def test_conflict_report_requires_explicit_shared_conflict_key(client):
    session_id = create_session(client)
    for item_id, content in (("d1", "Use port 9931."), ("d2", "Use port 9940.")):
        response = client.post("/v1/context/items", json={
            "session_id": session_id,
            "item": {
                "id": item_id,
                "type": "DECISION",
                "content": content,
                "metadata": {"conflict_key": "gateway_port"},
            },
        })
        assert response.status_code == 201

    result = client.post("/v1/memory/query", json={
        "session_id": session_id, "query": "What is the current gateway port?"
    }).json()
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["item_ids"] == "d1,d2"


def test_api_rejects_context_path_mismatch(client):
    session_id = create_session(client)
    response = client.put(f"/v1/sessions/{session_id}/context", json={
        "context": {"session_id": "another-session"},
    })
    assert response.status_code == 400


def test_chat_completion_injects_context_and_archives_turn(client, monkeypatch):
    session_id = create_session(client)
    previous = "Why did we choose SQLite for this project? We chose it because it is embedded and needs no separate server."
    client.post("/v1/messages", json={
        "session_id": session_id, "role": "user", "content": previous,
    })
    captured = {}

    def fake_complete(payload):
        captured.update(payload)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "SQLite is embedded."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr("c_link.api.app.complete_chat", fake_complete)
    response = client.post("/v1/chat/completions", json={
        "model": "local-model",
        "session_id": session_id,
        "messages": [{"role": "user", "content": "Why did we choose SQLite?"}],
    })

    assert response.status_code == 200
    result = response.json()
    assert result["choices"][0]["message"]["content"] == "SQLite is embedded."
    assert result["session_id"] == session_id
    assert result["c_link_memory"]["retrieval_performed"] is True
    assert previous in captured["messages"][0]["content"]
    assert "[CURRENT USER REQUEST]" not in captured["messages"][0]["content"]
    assert captured["messages"][-1]["content"] == "Why did we choose SQLite?"

    events = client.get(f"/v1/sessions/{session_id}/events").json()["events"]
    archived_roles = [event["payload"].get("role") for event in events if event["event_type"] == "MESSAGE_ARCHIVED"]
    assert archived_roles == ["assistant", "user", "user"]


def test_chat_completion_reports_provider_unavailable(client, monkeypatch):
    from c_link.providers.llamacpp import ProviderError

    session_id = create_session(client)
    monkeypatch.setattr(
        "c_link.api.app.complete_chat",
        lambda _payload: (_ for _ in ()).throw(ProviderError("local model server is offline")),
    )
    response = client.post("/v1/chat/completions", json={
        "session_id": session_id,
        "messages": [{"role": "user", "content": "Hello."}],
    })
    assert response.status_code == 502
    assert response.json()["detail"] == "local model server is offline"


def test_production_provider_errors_do_not_expose_internal_endpoints(client, monkeypatch):
    from c_link.providers.llamacpp import ProviderError

    monkeypatch.setenv("C_LINK_ENV", "production")
    session_id = create_session(client)
    monkeypatch.setattr(
        "c_link.api.app.complete_chat",
        lambda _payload: (_ for _ in ()).throw(ProviderError("failed at http://internal/private")),
    )
    response = client.post("/v1/chat/completions", json={
        "model": "local-model",
        "session_id": session_id,
        "messages": [{"role": "user", "content": "Hello."}],
    })
    assert response.status_code == 502
    assert "internal" not in response.text


def test_memory_query_can_force_current_mode_without_archive_search(client):
    session_id = create_session(client)
    client.post("/v1/messages", json={
        "session_id": session_id, "role": "user", "content": "Earlier history contains a private token."
    })
    response = client.post("/v1/memory/query", json={
        "session_id": session_id, "query": "What happened earlier?", "mode": "current"
    })
    assert response.status_code == 200
    assert response.json()["mode"] == "current"
    assert response.json()["retrieval_performed"] is False
    assert response.json()["evidence"] == []


def test_context_snapshot_can_be_restored_as_a_new_version(client):
    session_id = create_session(client)
    client.put(f"/v1/sessions/{session_id}/context", json={
        "reason": "first", "context": {"session_id": session_id, "current_objective": "First objective."}
    })
    client.put(f"/v1/sessions/{session_id}/context", json={
        "reason": "second", "context": {"session_id": session_id, "current_objective": "Second objective."}
    })
    snapshots = client.get(f"/v1/sessions/{session_id}/snapshots").json()["snapshots"]
    assert [snapshot["version"] for snapshot in snapshots[:3]] == [2, 1, 0]

    restored = client.post(f"/v1/sessions/{session_id}/context/restore", json={"version": 1})
    assert restored.status_code == 200
    assert restored.json()["context"]["current_objective"] == "First objective."
    assert restored.json()["context"]["context_version"] == 3


def test_streaming_chat_relays_sse_and_archives_complete_answer(client, monkeypatch):
    session_id = create_session(client)
    chunks = [
        {"id": "stream-1", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "Hello "}, "finish_reason": None}
        ]},
        {"id": "stream-1", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"content": "world"}, "finish_reason": None}
        ]},
        {"id": "stream-1", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {}, "finish_reason": "stop"}
        ]},
    ]
    payload = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    payload += b"data: [DONE]\n\n"
    captured = {}

    def fake_stream(body):
        captured.update(body)
        return BytesIO(payload)

    monkeypatch.setattr("c_link.api.app.open_chat_stream", fake_stream)
    response = client.post("/v1/chat/completions", json={
        "session_id": session_id,
        "stream": True,
        "top_p": 0.2,
        "memory_mode": "current",
        "messages": [{"role": "user", "content": "Hello?"}],
    })

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-c-link-session-id"] == session_id
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    assert events[0].get("session_id") == session_id, response.text
    assert events[0]["model"] == "local-model"
    assert events[0]["c_link_memory"]["mode"] == "current"
    assert captured["top_p"] == 0.2
    assert captured["stream"] is True
    archived = client.get(f"/v1/sessions/{session_id}/events").json()["events"]
    assert any(
        event["event_type"] == "MESSAGE_ARCHIVED" and event["payload"].get("role") == "assistant"
        for event in archived
    )


def test_api_key_protects_versioned_routes_but_keeps_health_available(client, monkeypatch):
    monkeypatch.setenv("C_LINK_AUTH_MODE", "api_key")
    store = get_store()
    tenant = store.create_tenant("Test tenant")
    key = store.issue_api_key(tenant["id"], "test key")["key"]
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 503


def test_tenant_api_key_is_hashed_and_can_be_revoked(client):
    store = get_store()
    tenant = store.create_tenant("Revocation test")
    issued = store.issue_api_key(tenant["id"], "temporary")
    assert store.authenticate_api_key(issued["key"]) == tenant["id"]
    assert store.revoke_api_key(issued["prefix"]) is True
    assert store.authenticate_api_key(issued["key"]) is None
    assert store.revoke_api_key(issued["prefix"]) is False
    with sqlite3.connect(store.database) as conn:
        (stored_hash,) = conn.execute("SELECT key_hash FROM api_keys WHERE prefix=?", (issued["prefix"],)).fetchone()
    assert stored_hash != issued["key"]


def test_tenant_suspension_and_erasure_revoke_keys_and_remove_owned_data(tmp_path):
    from c_link.storage.store import MemoryStore

    store = MemoryStore(tmp_path / "database.sqlite", tmp_path / "data")
    tenant = store.create_tenant("Erase me")
    issued = store.issue_api_key(tenant["id"], "test")
    store.create_session("erase-session", tenant_id=tenant["id"])
    store.append_message("erase-session", "user", "private content", tenant_id=tenant["id"])
    assert store.set_tenant_active(tenant["id"], False)
    assert store.authenticate_api_key(issued["key"]) is None
    assert store.delete_tenant(tenant["id"])
    assert [row["id"] for row in store.list_tenants()] == ["local"]
    assert not (tmp_path / "data" / "contexts" / "_tenants" / tenant["id"]).exists()


def test_authenticated_tenant_requests_are_rate_limited(client, monkeypatch):
    monkeypatch.setenv("C_LINK_AUTH_MODE", "api_key")
    monkeypatch.setenv("C_LINK_TENANT_REQUESTS_PER_MINUTE", "1")
    store = get_store()
    tenant = store.create_tenant("Rate limit test")
    key = store.issue_api_key(tenant["id"], "test")["key"]
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/models", headers=headers).status_code == 503
    limited = client.get("/v1/models", headers=headers)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"


def test_production_configuration_fails_closed_without_auth_and_proxy(monkeypatch):
    from c_link.api.app import _validate_production_settings

    monkeypatch.setenv("C_LINK_ENV", "production")
    monkeypatch.delenv("C_LINK_AUTH_MODE", raising=False)
    with pytest.raises(RuntimeError, match="C_LINK_AUTH_MODE"):
        _validate_production_settings()
    monkeypatch.setenv("C_LINK_AUTH_MODE", "api_key")
    with pytest.raises(RuntimeError, match="TLS-terminating"):
        _validate_production_settings()


def test_tenant_api_keys_cannot_read_or_mutate_another_tenants_session(client, monkeypatch):
    monkeypatch.setenv("C_LINK_AUTH_MODE", "api_key")
    store = get_store()
    alice = store.create_tenant("Alice")
    bob = store.create_tenant("Bob")
    alice_key = store.issue_api_key(alice["id"], "alice cli")["key"]
    bob_key = store.issue_api_key(bob["id"], "bob cli")["key"]
    session_id = "tenant-isolation-session"
    store.create_session(session_id, tenant_id=alice["id"])
    store.append_message(session_id, "user", "Alice private archive entry.", tenant_id=alice["id"])

    alice_headers = {"Authorization": f"Bearer {alice_key}"}
    bob_headers = {"Authorization": f"Bearer {bob_key}"}
    assert client.get(f"/v1/sessions/{session_id}/context", headers=alice_headers).status_code == 200
    assert client.get(f"/v1/sessions/{session_id}/context", headers=bob_headers).status_code == 404
    assert client.get(f"/v1/sessions/{session_id}/events", headers=bob_headers).status_code == 404
    assert client.get(f"/v1/sessions/{session_id}/messages", headers=bob_headers).status_code == 404
    assert client.get("/v1/sessions", headers=bob_headers).json()["sessions"] == []
    assert client.delete(f"/v1/sessions/{session_id}", headers=bob_headers).status_code == 404
    assert client.post("/v1/messages", headers=bob_headers, json={
        "session_id": session_id, "role": "user", "content": "Unauthorized change."
    }).status_code == 404
    assert store.search_messages(session_id, "Alice private archive", tenant_id=bob["id"]) == []
    owned = client.get(f"/v1/sessions/{session_id}/messages", headers=alice_headers)
    assert owned.status_code == 200
    assert owned.json()["messages"][0]["content"] == "Alice private archive entry."
    assert client.delete(f"/v1/sessions/{session_id}", headers=alice_headers).status_code == 204
    with pytest.raises(KeyError):
        store.get_context(session_id, tenant_id=alice["id"])


def test_model_list_hides_upstream_model_identity(client, monkeypatch):
    monkeypatch.setenv("C_LINK_API_KEY", "")
    monkeypatch.setattr("c_link.api.app.list_models", lambda: {"data": [{"id": "qwen"}]})
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "local-model"
    assert "qwen" not in response.text


def test_chat_rejects_unpublished_upstream_model_id(client):
    response = client.post("/v1/chat/completions", json={
        "model": "C:\\server\\private-model.gguf",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 404
    assert "private-model" not in response.text


def test_chat_uses_session_header_and_bounds_forwarded_history(client, monkeypatch):
    session_id = create_session(client)
    monkeypatch.setenv("C_LINK_MAX_RECENT_MESSAGES", "1")
    captured = {}

    def fake_complete(payload):
        captured.update(payload)
        return {
            "choices": [{"message": {"role": "assistant", "content": "Answer."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr("c_link.api.app.complete_chat", fake_complete)
    response = client.post("/v1/chat/completions", headers={"X-C-Link-Session-Id": session_id}, json={
        "messages": [
            {"role": "system", "content": "Follow this instruction."},
            {"role": "user", "content": "An old request."},
            {"role": "assistant", "content": "An old answer."},
            {"role": "user", "content": "Latest request."},
        ],
    })
    assert response.status_code == 200
    assert response.json()["model"] == "local-model"
    assert "qwen" not in response.text.casefold()
    assert response.json()["session_id"] == session_id
    assert response.json()["c_link_memory"]["messages_received"] == 4
    assert response.json()["c_link_memory"]["messages_forwarded"] == 2
    forwarded = captured["messages"]
    assert forwarded[-1]["content"] == "Latest request."
    assert not any(message.get("content") == "An old answer." for message in forwarded)
    assert any(message.get("role") == "system" and "Follow this instruction." in message["content"]
               for message in forwarded)


def test_chat_rejects_missing_session_from_affinity_header(client):
    response = client.post("/v1/chat/completions", headers={"X-C-Link-Session-Id": "unknown"}, json={
        "messages": [{"role": "user", "content": "Hello."}],
    })
    assert response.status_code == 404


def test_chat_can_report_llama_tokenizer_count(client, monkeypatch):
    session_id = create_session(client)
    monkeypatch.setenv("C_LINK_COUNT_CONTEXT_TOKENS", "true")
    monkeypatch.setattr("c_link.api.app.tokenize_text", lambda _content: 37)
    monkeypatch.setattr("c_link.api.app.complete_chat", lambda _payload: {
        "choices": [{"message": {"role": "assistant", "content": "OK."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
    })
    response = client.post("/v1/chat/completions", json={
        "session_id": session_id,
        "messages": [{"role": "user", "content": "What is next?"}],
    })
    assert response.status_code == 200
    assert response.json()["c_link_memory"]["context_packet_tokens"] == 37
