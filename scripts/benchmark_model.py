"""Compare raw model, C-Link current context, and archive-backed recall.

The math prompts are deterministic smoke cases. Archive recall is a separate
track so retrieval does not leak the answer into the math reasoning condition.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import platform
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from c_link.providers.llamacpp import iter_sse_data


def request_json(
    url: str,
    payload: dict | None = None,
    method: str | None = None,
    api_key: str | None = None,
) -> tuple[dict, float]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        url,
        data=data,
        headers=headers,
        method=method or ("GET" if data is None else "POST"),
    )
    started = perf_counter()
    try:
        with urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc
    return result, (perf_counter() - started) * 1000


def chat_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    return base_url + "/chat/completions" if base_url.endswith("/v1") else base_url + "/v1/chat/completions"


def run_streaming_chat(
    url: str,
    model: str,
    messages: list[dict],
    session_id: str | None = None,
    memory_mode: str | None = None,
    max_tokens: int = 512,
) -> tuple[dict, float, float | None]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if session_id:
        payload["session_id"] = session_id
    if memory_mode:
        payload["memory_mode"] = memory_mode
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    if session_id or memory_mode:
        api_key = os.environ.get("C_LINK_API_KEY")
    else:
        api_key = os.environ.get("C_LINK_LLAMACPP_API_KEY")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    started = perf_counter()
    first_token_ms = None
    content: list[str] = []
    usage: dict = {}
    timings: dict = {}
    memory: dict = {}
    try:
        with urlopen(request, timeout=300) as response:
            for data in iter_sse_data(response):
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("model endpoint returned malformed SSE JSON") from exc
                if chunk.get("error"):
                    raise RuntimeError(f"model endpoint stream error: {chunk['error']}")
                if isinstance(chunk.get("usage"), dict):
                    usage.update(chunk["usage"])
                if isinstance(chunk.get("timings"), dict):
                    timings.update(chunk["timings"])
                if isinstance(chunk.get("c_link_memory"), dict):
                    memory.update(chunk["c_link_memory"])
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        content.append(piece)
                        if first_token_ms is None:
                            first_token_ms = (perf_counter() - started) * 1000
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc

    elapsed_ms = (perf_counter() - started) * 1000
    result = {
        "choices": [{"message": {"role": "assistant", "content": "".join(content)}}],
        "usage": usage,
        "timings": timings,
        "c_link_memory": memory,
    }
    return result, elapsed_ms, first_token_ms


def summarize(result: dict, elapsed_ms: float, ttft_ms: float | None) -> dict:
    choices = result.get("choices") or []
    answer = choices[0].get("message", {}).get("content", "") if choices else ""
    usage = result.get("usage", {})
    timings = result.get("timings", {})
    memory = result.get("c_link_memory", {})
    completion_tokens = usage.get("completion_tokens")
    generation_seconds = max(0.001, elapsed_ms / 1000 - (ttft_ms or 0) / 1000)
    return {
        "answer": answer,
        "total_latency_ms": round(elapsed_ms, 2),
        "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "provider_generation_tokens_per_second": (
            timings.get("predicted_per_second")
            or timings.get("generation_tokens_per_second")
            or (round(completion_tokens / generation_seconds, 2) if completion_tokens else None)
        ),
        "c_link_mode": memory.get("mode"),
        "evidence_count": memory.get("evidence_count", 0),
        "retrieval_performed": memory.get("retrieval_performed", False),
        "context_packet_chars": memory.get("context_packet_chars"),
        "context_packet_tokens": memory.get("context_packet_tokens"),
        "client_messages_received": memory.get("messages_received"),
        "client_messages_forwarded": memory.get("messages_forwarded"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run streaming C-Link and raw-model comparisons.")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:9932")
    parser.add_argument("--c-link-base-url", default="http://127.0.0.1:9931")
    parser.add_argument("--model", default="local-model")
    parser.add_argument("--c-link-model", default="local-model")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--cases", type=Path, default=Path("benchmarks/math_cases.json"))
    parser.add_argument("--output", type=Path, default=Path("benchmark-results/model-baseline.json"))
    args = parser.parse_args()
    if args.max_tokens < 1:
        parser.error("max-tokens must be positive")

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    c_link_root = args.c_link_base_url.rstrip("/")
    raw_chat = chat_url(args.model_base_url)
    c_link_chat = c_link_root + "/v1/chat/completions"

    health, _ = request_json(c_link_root + "/health")
    if health.get("service") != "c-link":
        raise SystemExit(f"Not a C-Link API: {health}")
    request_json(
        raw_chat,
        {"model": args.model, "messages": [{"role": "user", "content": "Reply with OK."}],
         "temperature": 0, "max_tokens": 2},
        api_key=os.environ.get("C_LINK_LLAMACPP_API_KEY"),
    )

    session_id = "model-bench-" + str(uuid4())
    created, _ = request_json(c_link_root + "/v1/sessions", {
        "session_id": session_id,
        "project_id": "model-benchmark",
    }, api_key=os.environ.get("C_LINK_API_KEY"))
    context = created["context"]
    context["current_objective"] = "Answer benchmark questions accurately and concisely."
    context["current_state"] = "Math is evaluated without archive evidence; recall is evaluated separately."
    request_json(c_link_root + f"/v1/sessions/{session_id}/context", {
        "context": context,
        "reason": "model_benchmark_setup",
    }, method="PUT", api_key=os.environ.get("C_LINK_API_KEY"))

    archive_text = (
        "Why did we choose SQLite for the initial archive? We chose SQLite because it is embedded, "
        "durable, portable, and does not require a separate database service."
    )
    request_json(c_link_root + "/v1/messages", {
        "session_id": session_id,
        "role": "user",
        "content": archive_text,
        "source": "benchmark-seed",
    }, api_key=os.environ.get("C_LINK_API_KEY"))

    results = []
    for case in cases:
        question = case["question"]
        raw, raw_ms, raw_ttft = run_streaming_chat(
            raw_chat, args.model, [{"role": "user", "content": question}], max_tokens=args.max_tokens
        )
        with_context, context_ms, context_ttft = run_streaming_chat(
            c_link_chat,
            args.c_link_model,
            [{"role": "user", "content": question}],
            session_id=session_id,
            memory_mode="current",
            max_tokens=args.max_tokens,
        )
        results.append({
            "case_id": case["id"],
            "question": question,
            "expected_answer": case["expected_answer"],
            "grading_note": case.get("grading_note"),
            "raw_model": summarize(raw, raw_ms, raw_ttft),
            "c_link_context_only": summarize(with_context, context_ms, context_ttft),
            "manual_grading_required": True,
        })

    recall_question = "Why did we choose SQLite for the initial archive?"
    recall_context_session_id = "model-bench-context-" + str(uuid4())
    recall_retrieval_session_id = "model-bench-retrieval-" + str(uuid4())
    for recall_session in (recall_context_session_id, recall_retrieval_session_id):
        created, _ = request_json(c_link_root + "/v1/sessions", {
            "session_id": recall_session,
            "project_id": recall_session,
        }, api_key=os.environ.get("C_LINK_API_KEY"))
        recall_context = created["context"]
        recall_context["current_objective"] = "Recall the reason recorded in the archive."
        recall_context["current_state"] = "Controlled archive-recall comparison."
        request_json(c_link_root + f"/v1/sessions/{recall_session}/context", {
            "context": recall_context,
            "reason": "recall_benchmark_setup",
        }, method="PUT", api_key=os.environ.get("C_LINK_API_KEY"))
        request_json(c_link_root + "/v1/messages", {
            "session_id": recall_session,
            "role": "user",
            "content": archive_text,
            "source": "benchmark-seed",
        }, api_key=os.environ.get("C_LINK_API_KEY"))

    raw_recall, raw_recall_ms, raw_recall_ttft = run_streaming_chat(
        raw_chat, args.model, [{"role": "user", "content": recall_question}], max_tokens=args.max_tokens
    )
    context_recall, context_recall_ms, context_recall_ttft = run_streaming_chat(
        c_link_chat,
        args.c_link_model,
        [{"role": "user", "content": recall_question}],
        session_id=recall_context_session_id,
        memory_mode="current",
        max_tokens=args.max_tokens,
    )
    retrieved_recall, retrieved_recall_ms, retrieved_recall_ttft = run_streaming_chat(
        c_link_chat,
        args.c_link_model,
        [{"role": "user", "content": recall_question}],
        session_id=recall_retrieval_session_id,
        memory_mode="why",
        max_tokens=args.max_tokens,
    )
    archive_recall = {
        "question": recall_question,
        "expected_answer": "SQLite was chosen because it is embedded, durable, portable, and needs no separate service.",
        "raw_model": summarize(raw_recall, raw_recall_ms, raw_recall_ttft),
        "c_link_context_only": summarize(context_recall, context_recall_ms, context_recall_ttft),
        "c_link_context_plus_archive_retrieval": summarize(
            retrieved_recall, retrieved_recall_ms, retrieved_recall_ttft
        ),
        "manual_grading_required": True,
    }

    report = {
        "benchmark": "c-link-model-v2",
        "c_link_version": __import__("c_link").__version__,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "model": Path(args.model).name,
        "c_link_model": args.c_link_model,
        "model_base_url": args.model_base_url,
        "c_link_base_url": args.c_link_base_url,
        "max_tokens": args.max_tokens,
        "streaming": True,
        "session_id": session_id,
        "recall_context_only_session_id": recall_context_session_id,
        "recall_retrieval_session_id": recall_retrieval_session_id,
        "cases": results,
        "archive_recall": archive_recall,
        "notes": [
            "Raw and C-Link context-only math calls use the same model, user text, temperature, and token limit.",
            "The archive recall track compares raw, current-context-only, and forced why-mode retrieval on the same question.",
            "The context-only and retrieval conditions use separate sessions with identical seed data to prevent benchmark turns from leaking answers between conditions.",
            "The math prompts are generated smoke cases until the exact project-approved questions are supplied.",
            "TTFT is time until the first non-empty streamed text delta; prompt/completion token counts depend on provider usage support.",
            "Keep llama.cpp build, hardware, model quantization, context length, GPU offload, and runtime flags constant across runs.",
            "RAM/VRAM sampling is saved separately; run multiple times before setting performance targets.",
        ],
    }
    rendered = json.dumps(report, indent=2)
    print(rendered)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(f"Report written to {args.output.resolve()}")


if __name__ == "__main__":
    main()
