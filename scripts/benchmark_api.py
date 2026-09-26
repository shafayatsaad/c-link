"""Measure end-to-end C-Link HTTP ingestion and memory-query latency.

Example (with C-Link already running):
    python scripts/benchmark_api.py --base-url http://127.0.0.1:9931 --messages 500

The benchmark creates a dedicated session and leaves its messages in the configured
archive. Use a fresh database if you want a disposable run.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import platform
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


QUERY = "Why did we choose SQLite for the initial archive decision?"
TARGET = (
    "Why did we choose SQLite for the initial archive decision? "
    "SQLite was selected because it is embedded, durable, and needs no separate server."
)


def request_json(base_url: str, path: str, body: dict | None = None) -> tuple[dict, float]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    api_key = os.environ.get("C_LINK_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method="GET" if data is None else "POST",
    )
    started = perf_counter()
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"C-Link returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach C-Link at {base_url}: {exc.reason}") from exc
    return payload, (perf_counter() - started) * 1000


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(len(ordered) * fraction + 0.999999)))
    return ordered[rank - 1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark C-Link's end-to-end HTTP memory path.")
    parser.add_argument("--base-url", default="http://127.0.0.1:9931")
    parser.add_argument("--messages", type=int, default=500)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.messages < 1 or args.queries < 1 or args.warmups < 0:
        parser.error("messages and queries must be positive; warmups cannot be negative")

    health, _ = request_json(args.base_url, "/health")
    if health.get("service") != "c-link" or health.get("memory") != "ready":
        raise SystemExit(f"Endpoint is not a C-Link memory API: {health}")

    session_id = "http-bench-" + str(uuid4())
    created, create_ms = request_json(args.base_url, "/v1/sessions", {
        "session_id": session_id,
        "project_id": "benchmark",
    })
    session_id = created["session_id"]

    write_latencies: list[float] = []
    started_seed = perf_counter()
    target_id = None
    for index in range(args.messages):
        content = TARGET if index == 0 else f"Unrelated archived turn {index + 1}."
        message, elapsed_ms = request_json(args.base_url, "/v1/messages", {
            "session_id": session_id,
            "role": "user",
            "content": content,
            "source": "benchmark",
        })
        write_latencies.append(elapsed_ms)
        if index == 0:
            target_id = message["message"]["id"]
    seed_seconds = perf_counter() - started_seed

    query_body = {"session_id": session_id, "query": QUERY, "limit": 5}
    timed_queries: list[float] = []
    last_result = None
    for index in range(args.warmups + args.queries):
        last_result, elapsed_ms = request_json(args.base_url, "/v1/memory/query", query_body)
        if index >= args.warmups:
            timed_queries.append(elapsed_ms)

    target_recalled = any(item["message_id"] == target_id for item in last_result["evidence"])
    report = {
        "benchmark": "c-link-http-memory-v1",
        "c_link_version": __import__("c_link").__version__,
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "base_url": args.base_url,
        "authenticated": bool(os.environ.get("C_LINK_API_KEY")),
        "tenant_requests_per_minute_limit": os.environ.get("C_LINK_TENANT_REQUESTS_PER_MINUTE", "default"),
        "session_id": session_id,
        "message_count": args.messages,
        "session_create_ms": round(create_ms, 3),
        "ingest_total_seconds": round(seed_seconds, 3),
        "ingest_messages_per_second": round(args.messages / seed_seconds, 2) if seed_seconds else None,
        "write_p50_ms": round(percentile(write_latencies, 0.50), 3),
        "write_p95_ms": round(percentile(write_latencies, 0.95), 3),
        "query_repeats": args.queries,
        "query_warmups_discarded": args.warmups,
        "query_p50_ms": round(percentile(timed_queries, 0.50), 3),
        "query_p95_ms": round(percentile(timed_queries, 0.95), 3),
        "target_recalled": target_recalled,
        "evidence_count": len(last_result["evidence"]),
        "retrieval_methods": sorted({item["retrieval_method"] for item in last_result["evidence"]}),
        "context_packet_chars": len(last_result["context_packet"]),
        "model_involved": False,
        "benchmark_session_persists": True,
    }
    output = json.dumps(report, indent=2)
    print(output)
    if args.output:
        from pathlib import Path
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
        print(f"Report written to {path.resolve()}")

    if not target_recalled:
        raise SystemExit("Benchmark failed its archive-recall gate: seeded evidence was not retrieved.")


if __name__ == "__main__":
    main()
