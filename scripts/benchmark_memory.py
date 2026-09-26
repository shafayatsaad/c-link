"""Repeatable, model-free benchmark for SQLite archive and bounded context.

Example (from the production directory):
    python scripts/benchmark_memory.py --sizes 500,2000,10000 --repeats 50

All data is written to a temporary directory and removed when the run ends.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import UTC, datetime
import json
import platform
import sqlite3
import tempfile
from pathlib import Path
from statistics import mean
from time import perf_counter

from c_link.context.compiler import compile_context
from c_link.context.schema import ActiveContext, ContextItem, ItemType
from c_link.storage.db import connect, transaction
from c_link.storage.store import MemoryStore, render_context


QUERY = "Why did we choose SQLite for the initial archive decision?"
TARGET = (
    "Why did we choose SQLite for the initial archive decision? "
    "SQLite was selected because it is embedded, durable, and needs no separate server."
)
OBJECTIVE = "Preserve a bounded active context while archiving the complete conversation."


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * fraction + 0.999999)) - 1))
    return ordered[index]


def run_case(root: Path, count: int, repeats: int, warmups: int) -> dict[str, object]:
    case_dir = root / f"messages_{count}"
    case_dir.mkdir(parents=True)
    database = case_dir / "benchmark.sqlite"
    data_dir = case_dir / "data"
    store = MemoryStore(database, data_dir)
    session_id = f"bench-{count}"
    store.create_session(session_id, project_id="benchmark")
    context = store.get_context(session_id)
    context.current_objective = OBJECTIVE
    context.current_state = f"Archive contains {count} turns; canonical state remains compact."
    context.open_tasks = ["Validate retrieval recall", "Measure bounded packet size"]
    context.items.append(ContextItem(
        id="decision-sqlite",
        type=ItemType.DECISION,
        content="Use SQLite for the initial archive.",
        reason="It is embedded and durable without a separate service.",
        source="benchmark-seed",
        evidence_refs=["seed-message-1"],
    ))
    store.update_context(context, reason="benchmark_seed")

    # Seed through SQLite in one transaction. This isolates retrieval scaling from
    # HTTP client overhead and per-request connection setup.
    now = datetime.now(UTC).isoformat()
    seed_started = perf_counter()
    with transaction(database) as conn:
        cur = conn.execute(
            "INSERT INTO messages(session_id, role, content, created_at, source) "
            "VALUES (?, 'user', ?, ?, 'benchmark')",
            (session_id, TARGET, now),
        )
        target_id = cur.lastrowid
        remaining = max(0, count - 1)
        conn.executemany(
            "INSERT INTO messages(session_id, role, content, created_at, source) "
            "VALUES (?, 'user', ?, ?, 'benchmark')",
            ((session_id, f"Unrelated archived turn {i}: routine project discussion.", now)
             for i in range(remaining)),
        )
    seed_seconds = perf_counter() - seed_started

    # Close/checkpoint so the reported database size includes the full FTS index.
    with closing(connect(database)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    samples_ms: list[float] = []
    recall = False
    retrieval_method = "unknown"
    for index in range(warmups + repeats):
        started = perf_counter()
        hits = store.search_messages(session_id, QUERY, limit=5)
        elapsed = (perf_counter() - started) * 1000
        if index >= warmups:
            samples_ms.append(elapsed)
        if hits:
            recall = recall or any(hit["id"] == target_id for hit in hits)
            retrieval_method = hits[0]["retrieval_method"]

    # Keep canonical state fixed while the external archive grows. Packet size
    # should remain bounded independently of the archived message count.
    active = store.get_context(session_id)
    packet = compile_context(active, "What is the current objective?", max_chars=12000)
    projection = render_context(active)
    with closing(connect(database)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = database.stat().st_size

    return {
        "archived_messages": count,
        "bulk_seed_seconds": round(seed_seconds, 6),
        "bulk_seed_messages_per_second": round(count / seed_seconds, 2) if seed_seconds else None,
        "query_repeats": repeats,
        "query_warmups_discarded": warmups,
        "query_p50_ms": round(percentile(samples_ms, 0.50), 3),
        "query_p95_ms": round(percentile(samples_ms, 0.95), 3),
        "target_recalled": recall,
        "retrieval_method": retrieval_method,
        "active_context_json_chars": len(active.model_dump_json()),
        "compiled_packet_chars": len(packet),
        "packet_limit_chars": 12000,
        "packet_within_limit": len(packet) <= 12000,
        "context_projection_has_objective": OBJECTIVE in projection,
        "database_bytes": database_bytes,
    }


def parse_sizes(value: str) -> list[int]:
    try:
        sizes = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated positive integers") from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be comma-separated positive integers")
    return sizes


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark C-Link's provider-independent memory core.")
    parser.add_argument("--sizes", type=parse_sizes, default=parse_sizes("500,2000,10000"),
                        help="archive sizes to measure (default: 500,2000,10000)")
    parser.add_argument("--repeats", type=int, default=50, help="timed queries per archive size")
    parser.add_argument("--warmups", type=int, default=5, help="untimed warm-up queries")
    parser.add_argument("--output", type=Path, help="optional JSON report output path")
    args = parser.parse_args()
    if args.repeats < 1 or args.warmups < 0:
        parser.error("repeats must be positive and warmups cannot be negative")

    with tempfile.TemporaryDirectory(prefix="c_link_bench_") as temp:
        root = Path(temp)
        cases = [run_case(root, size, args.repeats, args.warmups) for size in args.sizes]
        report = {
            "benchmark": "c-link-memory-v1",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "retrieval_query": QUERY,
            "database_isolated_temporary": True,
            "cases": cases,
        }
        rendered = json.dumps(report, indent=2)
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
            print(f"Report written to {args.output.resolve()}")


if __name__ == "__main__":
    main()
