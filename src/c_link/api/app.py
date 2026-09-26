"""HTTP interface for C-Link's provider-independent memory core."""

from functools import lru_cache
from contextlib import asynccontextmanager
from ipaddress import ip_address
import json
import logging
import os
from pathlib import Path
import re
from collections import deque
from threading import BoundedSemaphore, Lock
import time
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from c_link.context.compiler import compile_context
from c_link.context.schema import ActiveContext, ContextItem
from c_link.storage.store import ContextConflict, MemoryStore, evidence_window
from c_link.providers.llamacpp import (
    ProviderError,
    complete_chat,
    iter_sse_data,
    list_models,
    open_chat_stream,
    provider_base_url,
    tokenize_text,
)

logger = logging.getLogger("c_link.api")
_rate_lock = Lock()
_request_times: dict[str, deque[float]] = {}
_generation_slots = BoundedSemaphore(max(1, int(os.environ.get("C_LINK_MAX_CONCURRENT_GENERATIONS", "1"))))


def _data_paths() -> tuple[Path, Path]:
    data_dir = Path(os.environ.get("C_LINK_DATA_DIR", "data")).expanduser()
    database = Path(os.environ.get("C_LINK_DATABASE", str(data_dir / "c_link.db"))).expanduser()
    return data_dir, database


@lru_cache(maxsize=1)
def get_store() -> MemoryStore:
    data_dir, database = _data_paths()
    return MemoryStore(database, data_dir)


def _validate_production_settings() -> None:
    if os.environ.get("C_LINK_ENV", "development").casefold() != "production":
        return
    if os.environ.get("C_LINK_AUTH_MODE", "").casefold() != "api_key":
        raise RuntimeError("production requires C_LINK_AUTH_MODE=api_key")
    if os.environ.get("C_LINK_BEHIND_TLS_PROXY", "").casefold() not in {"1", "true", "yes"}:
        raise RuntimeError("production must run behind a TLS-terminating reverse proxy")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _validate_production_settings()
    yield


app = FastAPI(
    title="C-Link",
    version="0.5.0",
    description="Provider-independent context and memory gateway.",
    lifespan=lifespan,
)

_cors_origins = [
    item.strip() for item in os.environ.get("C_LINK_CORS_ORIGINS", "").split(",") if item.strip()
]
if "*" in _cors_origins:
    raise RuntimeError("C_LINK_CORS_ORIGINS must list explicit browser origins; wildcard CORS is disabled")
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["DELETE", "GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-C-Link-Session-Id"],
        expose_headers=["X-C-Link-Session-Id"],
    )


@app.middleware("http")
async def tenant_auth_middleware(request: Request, call_next):
    """Authenticate each tenant API key and attach its isolated tenant ID."""
    body_limit = max(65536, min(int(os.environ.get("C_LINK_MAX_REQUEST_BYTES", "2097152")), 16_777_216))
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > body_limit:
                return JSONResponse(status_code=413, content={"detail": "request body exceeds configured limit"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid content-length"})
    request.state.tenant_id = "local"
    if request.url.path.startswith("/v1/") and request.method != "OPTIONS":
        mode = os.environ.get("C_LINK_AUTH_MODE", "local").casefold()
        if mode == "api_key":
            authorization = request.headers.get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            tenant_id = None
            if scheme.casefold() == "bearer" and supplied:
                try:
                    tenant_id = get_store().authenticate_api_key(supplied)
                except Exception:
                    logger.exception("API key authentication lookup failed")
            if tenant_id is None:
                logger.warning("Rejected API-key request path=%s", request.url.path)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "valid API key required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.tenant_id = tenant_id
        elif mode != "local":
            logger.error("Unknown C_LINK_AUTH_MODE")
            return JSONResponse(status_code=503, content={"detail": "authentication is misconfigured"})
        if mode == "api_key":
            rpm = max(1, min(int(os.environ.get("C_LINK_TENANT_REQUESTS_PER_MINUTE", "60")), 6000))
            now = time.monotonic()
            with _rate_lock:
                times = _request_times.setdefault(request.state.tenant_id, deque())
                while times and now - times[0] >= 60:
                    times.popleft()
                if len(times) >= rpm:
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "tenant request rate limit exceeded"},
                        headers={"Retry-After": "60"},
                    )
                times.append(now)
                if len(_request_times) > 10000:
                    stale = [key for key, values in _request_times.items() if not values or now - values[-1] >= 60]
                    for key in stale:
                        _request_times.pop(key, None)
        elif request.client:
            try:
                remote_is_loopback = ip_address(request.client.host).is_loopback
            except ValueError:
                remote_is_loopback = request.client.host.casefold() == "localhost"
            if not remote_is_loopback:
                return JSONResponse(status_code=403, content={"detail": "remote access requires API-key mode"})
    return await call_next(request)


class CreateSessionRequest(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)


class MessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=1_000_000)
    source: str = Field(default="api", max_length=100)


class AddContextItemRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    item: ContextItem


class ContextUpdateRequest(BaseModel):
    context: ActiveContext
    reason: str = Field(default="context_updated", max_length=200)


class RestoreSnapshotRequest(BaseModel):
    version: int = Field(ge=0)


class CompileRequest(BaseModel):
    context: ActiveContext | None = None
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    request: str
    max_chars: int = Field(default=12000, ge=64, le=100000)


class MemoryQueryRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=4000)
    project_id: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)
    evidence_chars: int = Field(default=1200, ge=100, le=10000)
    max_chars: int = Field(default=12000, ge=64, le=100000)
    mode: Literal["auto", "current", "why", "historical"] = "auto"


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=100_000)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=32768)
    stream: bool = False
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)
    max_context_chars: int = Field(default=12000, ge=256, le=100000)
    memory_mode: Literal["auto", "current", "why", "historical"] = "auto"


@app.get("/health")
def health(request: Request):
    try:
        get_store()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="memory store unavailable") from exc
    result = {
        "status": "ok",
        "service": "c-link",
        "memory": "ready",
        "provider": "llama.cpp",
        "provider_check": "not_performed",
    }
    is_loopback = False
    if request.client:
        try:
            is_loopback = ip_address(request.client.host).is_loopback
        except ValueError:
            is_loopback = request.client.host.casefold() == "localhost"
    if is_loopback and os.environ.get("C_LINK_ENV", "development").casefold() != "production":
        result["provider_url"] = provider_base_url()
    return result


@app.get("/health/provider")
def provider_health():
    try:
        models = list_models()
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail="model provider unavailable") from exc
    return {"status": "ok", "provider": "llama.cpp", "model_count": len(models["data"])}


@app.get("/v1/models")
def models():
    try:
        provider_models = list_models().get("data", [])
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail="model provider unavailable") from exc
    if not provider_models:
        raise HTTPException(status_code=503, detail="model provider has no loaded model")
    return {
        "object": "list",
        "data": [{
            "id": os.environ.get("C_LINK_MODEL_NAME", "local-model"),
            "object": "model",
            "created": int(provider_models[0].get("created", 0) or 0),
            "owned_by": "c-link",
        }],
    }


@app.post("/v1/sessions", status_code=201)
def create_session(req: CreateSessionRequest, request: Request):
    session_id = req.session_id or str(uuid4())
    try:
        context = get_store().create_session(session_id, req.project_id, request.state.tenant_id)
    except ContextConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session_id": session_id, "context": context.model_dump(mode="json")}


@app.get("/v1/sessions")
def list_sessions(request: Request, limit: int = 100):
    return {"sessions": get_store().list_sessions(request.state.tenant_id, limit)}


@app.delete("/v1/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request):
    if not get_store().delete_session(session_id, request.state.tenant_id):
        raise HTTPException(status_code=404, detail="session not found")
    return Response(status_code=204)


@app.get("/v1/sessions/{session_id}/messages")
def list_messages(
    session_id: str,
    request: Request,
    limit: int = 100,
    before_id: int | None = None,
):
    try:
        messages = get_store().list_messages(session_id, request.state.tenant_id, limit, before_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {
        "messages": messages,
        "next_before_id": messages[0]["id"] if messages else None,
    }


@app.get("/v1/sessions/{session_id}/context")
def read_context(session_id: str, request: Request):
    try:
        context = get_store().get_context(session_id, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"context": context.model_dump(mode="json")}


@app.put("/v1/sessions/{session_id}/context")
def update_context(session_id: str, req: ContextUpdateRequest, request: Request):
    if req.context.session_id != session_id:
        raise HTTPException(status_code=400, detail="context session_id does not match URL")
    try:
        context = get_store().update_context(req.context, req.reason, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"context": context.model_dump(mode="json")}


@app.get("/v1/sessions/{session_id}/snapshots")
def list_context_snapshots(session_id: str, request: Request, limit: int = 100):
    try:
        get_store().get_context(session_id, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"snapshots": get_store().list_snapshots(session_id, limit, request.state.tenant_id)}


@app.post("/v1/sessions/{session_id}/context/restore")
def restore_context_snapshot(session_id: str, req: RestoreSnapshotRequest, request: Request):
    try:
        context = get_store().restore_snapshot(session_id, req.version, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="snapshot not found") from exc
    return {"context": context.model_dump(mode="json")}


@app.post("/v1/messages", status_code=201)
def archive_message(req: MessageRequest, request: Request):
    try:
        message = get_store().append_message(
            req.session_id, req.role, req.content, req.source, request.state.tenant_id
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"message": message}


@app.post("/v1/context/items", status_code=201)
def add_context_item(req: AddContextItemRequest, request: Request):
    try:
        context = get_store().add_context_item(req.session_id, req.item, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    except ContextConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"context": context.model_dump(mode="json")}


@app.get("/v1/sessions/{session_id}/events")
def read_events(session_id: str, request: Request, limit: int = 100):
    try:
        get_store().get_context(session_id, request.state.tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return {"events": get_store().list_events(session_id, limit, request.state.tenant_id)}


@app.post("/v1/context/compile")
def compile(req: CompileRequest, request: Request):
    if req.context is not None:
        context = req.context
    elif req.session_id:
        try:
            context = get_store().get_context(req.session_id, request.state.tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
    else:
        raise HTTPException(status_code=422, detail="provide context or session_id")
    return {"context_packet": compile_context(context, req.request, req.max_chars)}


def _query_mode(query: str) -> str:
    text = query.casefold()
    historical = ("historical", "history", "before", "previous", "earlier", "originally",
                  "superseded", "used to", "what did we decide", "what was the decision")
    if any(phrase in text for phrase in historical):
        return "historical"
    if re.search(r"\bwhy\b|\breason\b|\bjustification\b", text):
        return "why"
    return "current"


def _conflicts(context: ActiveContext) -> list[dict[str, str]]:
    """Report explicit same-key conflicts without guessing semantic equivalence."""
    groups: dict[tuple[str, str], list[ContextItem]] = {}
    for item in context.items:
        key = item.metadata.get("conflict_key")
        if key and item.status.value in {"ACTIVE", "OPEN"}:
            groups.setdefault((item.type.value, str(key)), []).append(item)
    return [
        {"type": item_type, "conflict_key": key,
         "item_ids": ",".join(item.id for item in items),
         "contents": " | ".join(item.content for item in items)}
        for (item_type, key), items in groups.items()
        if len({item.content for item in items}) > 1
    ]


def _query_memory_for_tenant(req: MemoryQueryRequest, tenant_id: str) -> dict:
    store = get_store()
    try:
        context = store.get_context(req.session_id, tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    mode = _query_mode(req.query) if req.mode == "auto" else req.mode
    results = []
    # Current-state questions use canonical state. Historical and causal questions
    # retrieve source messages so the response packet can retain evidence.
    if mode in {"historical", "why"}:
        results = store.search_messages(
            req.session_id, req.query, req.limit, req.project_id or context.project_id, tenant_id
        )
    evidence = []
    for result in results:
        excerpt = evidence_window(result["content"], req.query, req.evidence_chars)
        evidence.append({
            "message_id": result["id"],
            "session_id": result["session_id"],
            "role": result["role"],
            "created_at": result["created_at"],
            "excerpt": excerpt,
            "retrieval_method": result["retrieval_method"],
        })
    evidence_header = "\n\n[RETRIEVED HISTORICAL EVIDENCE]\n"
    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- message {item['message_id']} (session {item['session_id']}, {item['created_at']}): {item['excerpt']}"
            for item in evidence
        )
        # Keep current request and canonical state in the packet; evidence gets a
        # bounded share of the character budget and can never push the packet over it.
        evidence_budget = req.max_chars // 3
        evidence_text = evidence_text[:evidence_budget]
        context_budget = max(64, req.max_chars - len(evidence_header) - len(evidence_text))
        if context_budget + len(evidence_header) + len(evidence_text) > req.max_chars:
            evidence_text = evidence_text[:max(0, req.max_chars - context_budget - len(evidence_header))]
    else:
        context_budget = req.max_chars
    packet = compile_context(context, req.query, context_budget)
    if evidence_text:
        packet += evidence_header + evidence_text
    return {
        "mode": mode,
        "context_packet": packet,
        "evidence": evidence,
        "conflicts": _conflicts(context),
        "retrieval_performed": mode in {"historical", "why"},
    }


@app.post("/v1/memory/query")
def query_memory(req: MemoryQueryRequest, request: Request):
    return _query_memory_for_tenant(req, request.state.tenant_id)


def _stream_completion(
    upstream, store: MemoryStore, session_id: str, tenant_id: str, memory_meta: dict,
    generation_slots: BoundedSemaphore, public_model_name: str,
):
    """Relay OpenAI SSE chunks and persist only a completed assistant response."""
    text_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    metadata_attached = False
    completed = False
    clean_eof = True
    try:
        for data in iter_sse_data(upstream):
            if data.strip() == "[DONE]":
                completed = True
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                yield f"data: {data}\n\n"
                continue
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                for tool_delta in delta.get("tool_calls", []):
                    index = int(tool_delta.get("index", 0))
                    tool = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tool_delta.get("id"):
                        tool["id"] = tool_delta["id"]
                    if tool_delta.get("type"):
                        tool["type"] = tool_delta["type"]
                    function_delta = tool_delta.get("function", {})
                    if function_delta.get("name"):
                        tool["function"]["name"] += function_delta["name"]
                    if function_delta.get("arguments"):
                        tool["function"]["arguments"] += function_delta["arguments"]
            event["model"] = public_model_name
            if not metadata_attached:
                event["session_id"] = session_id
                event["c_link_memory"] = memory_meta
                metadata_attached = True
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    except Exception:
        clean_eof = False
        logger.exception("llama.cpp streaming response failed")
        yield 'event: error\ndata: {"error":{"message":"upstream stream failed","type":"server_error"}}\n\n'
    finally:
        upstream.close()
        generation_slots.release()
    if clean_eof and (completed or text_parts):
        answer = "".join(text_parts)
        if not answer and tool_calls:
            answer = json.dumps({"tool_calls": [tool_calls[index] for index in sorted(tool_calls)]})
        if answer:
            store.append_message(session_id, "assistant", answer, source="llamacpp", tenant_id=tenant_id)
    yield "data: [DONE]\n\n"


def _bounded_messages(messages: list[ChatMessage]) -> tuple[list[dict], dict[str, int]]:
    """Keep instructions and a bounded newest conversation window for the model."""
    max_count = max(1, min(int(os.environ.get("C_LINK_MAX_RECENT_MESSAGES", "12")), 32))
    max_chars = max(2048, min(int(os.environ.get("C_LINK_MAX_RECENT_CHARS", "8000")), 80000))
    instructions = [message for message in messages if message.role in {"system", "developer"}]
    conversation = [message for message in messages if message.role not in {"system", "developer"}]
    instruction_chars = sum(len(message.content or "") for message in instructions)
    if instruction_chars >= max_chars:
        raise HTTPException(status_code=413, detail="system and developer messages exceed the configured prompt budget")
    remaining_chars = max_chars - instruction_chars
    latest_user = next((message for message in reversed(conversation) if message.role == "user"), None)
    selected_reversed: list[ChatMessage] = []
    used_chars = 0
    if latest_user is not None:
        if len(latest_user.content or "") > remaining_chars:
            raise HTTPException(status_code=413, detail="latest user message exceeds the configured prompt budget")
        selected_reversed.append(latest_user)
        used_chars = len(latest_user.content or "")
    for message in reversed(conversation):
        if message is latest_user:
            continue
        if len(selected_reversed) >= max_count:
            break
        if used_chars + len(message.content or "") > remaining_chars:
            break
        selected_reversed.append(message)
        used_chars += len(message.content or "")
    selected = instructions + list(reversed(selected_reversed))
    selected_ids = {id(message) for message in selected}
    ordered = [message for message in messages if id(message) in selected_ids]
    return [message.model_dump(exclude_none=True) for message in ordered], {
        "messages_received": len(messages),
        "messages_forwarded": len(ordered),
        "history_chars_forwarded": instruction_chars + used_chars,
    }


@app.post("/v1/chat/completions")
def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    x_c_link_session_id: str | None = Header(default=None, alias="X-C-Link-Session-Id"),
):
    """OpenAI-compatible chat endpoint backed by llama.cpp."""
    latest_user = next((message for message in reversed(req.messages) if message.role == "user"), None)
    if latest_user is None or latest_user.content is None:
        raise HTTPException(status_code=400, detail="messages must include a user message")
    public_model_name = os.environ.get("C_LINK_MODEL_NAME", "local-model")
    if req.model and req.model != public_model_name:
        raise HTTPException(status_code=404, detail="requested model is not available")
    if os.environ.get("C_LINK_ENV", "development").casefold() == "production":
        max_output = max(1, min(int(os.environ.get("C_LINK_MAX_OUTPUT_TOKENS", "4096")), 32768))
        if req.max_tokens > max_output:
            raise HTTPException(status_code=422, detail=f"max_tokens exceeds service limit ({max_output})")

    store = get_store()
    tenant_id = request.state.tenant_id
    supplied_session_id = req.session_id or x_c_link_session_id
    if supplied_session_id:
        try:
            context = store.get_context(supplied_session_id, tenant_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        session_id = supplied_session_id
    else:
        session_id = str(uuid4())
        try:
            context = store.create_session(session_id, req.project_id, tenant_id)
        except ContextConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Retrieve before archiving this turn, so a historical query cannot retrieve
    # its own text as if it were prior evidence.
    memory_request = MemoryQueryRequest(
        session_id=session_id,
        query=latest_user.content,
        project_id=req.project_id or context.project_id,
        max_chars=req.max_context_chars,
        mode=req.memory_mode,
    )
    # Reuse the authenticated principal for the same-tenant memory lookup.
    memory = _query_memory_for_tenant(memory_request, tenant_id)
    try:
        store.append_message(session_id, "user", latest_user.content, source="openai_api", tenant_id=tenant_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc

    request_section = f"\n\n[CURRENT USER REQUEST]\n{latest_user.content}"
    context_packet = memory["context_packet"].replace(request_section, "", 1)
    provider_messages = [{"role": "system", "content": context_packet}]
    bounded_messages, history_metrics = _bounded_messages(req.messages)
    provider_messages.extend(bounded_messages)
    custom_fields = {"session_id", "project_id", "max_context_chars", "memory_mode"}
    payload = req.model_dump(exclude_none=True, exclude=custom_fields)
    payload["model"] = os.environ.get("C_LINK_UPSTREAM_MODEL_NAME", public_model_name)
    payload["messages"] = provider_messages
    memory_meta = {
        "mode": memory["mode"],
        "evidence_count": len(memory["evidence"]),
        "retrieval_performed": memory["retrieval_performed"],
        "conflicts": memory["conflicts"],
        "context_packet_chars": len(context_packet),
        **history_metrics,
    }
    if os.environ.get("C_LINK_COUNT_CONTEXT_TOKENS", "false").casefold() in {"1", "true", "yes", "on"}:
        try:
            memory_meta["context_packet_tokens"] = tokenize_text(context_packet)
        except ProviderError:
            logger.warning("Could not count C-Link context packet tokens", exc_info=True)
    if req.stream:
        if not _generation_slots.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="model concurrency limit reached; retry shortly")
        try:
            upstream = open_chat_stream(payload)
        except ProviderError as exc:
            _generation_slots.release()
            if os.environ.get("C_LINK_ENV", "development").casefold() == "production":
                logger.error("Model provider streaming request failed: %s", exc)
                detail = "configured model provider request failed"
            else:
                detail = str(exc)
            raise HTTPException(status_code=502, detail=detail) from exc
        return StreamingResponse(
            _stream_completion(
                upstream, store, session_id, tenant_id, memory_meta, _generation_slots, public_model_name
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-C-Link-Session-Id": session_id,
            },
        )
    if not _generation_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="model concurrency limit reached; retry shortly")
    try:
        response = complete_chat(payload)
    except ProviderError as exc:
        if os.environ.get("C_LINK_ENV", "development").casefold() == "production":
            logger.error("Model provider request failed: %s", exc)
            detail = "configured model provider request failed"
        else:
            detail = str(exc)
        raise HTTPException(status_code=502, detail=detail) from exc
    finally:
        _generation_slots.release()
    try:
        message = response["choices"][0]["message"]
        answer = message.get("content")
        if answer is None and message.get("tool_calls"):
            answer = json.dumps({"tool_calls": message["tool_calls"]}, ensure_ascii=False)
        if not isinstance(answer, str):
            raise TypeError("assistant completion contained neither text nor tool calls")
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="llama.cpp returned no text answer") from exc

    store.append_message(session_id, "assistant", answer, source="llamacpp", tenant_id=tenant_id)
    response["model"] = public_model_name
    response["session_id"] = session_id
    response["c_link_memory"] = memory_meta
    return response
