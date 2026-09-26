import sqlite3

import pytest

from c_link.context.compiler import compile_context
from c_link.context.schema import ActiveContext, ContextItem, ItemStatus, ItemType
from c_link.storage.store import ContextConflict, MemoryStore, evidence_window


def decision(item_id: str, content: str, *, supersedes: str | None = None) -> ContextItem:
    return ContextItem(
        id=item_id,
        type=ItemType.DECISION,
        content=content,
        reason="Chosen for the initial implementation.",
        source="user",
        evidence_refs=["message-1"],
        supersedes=supersedes,
    )


def test_session_context_archive_events_and_projection_survive_reopen(tmp_path):
    database = tmp_path / "c_link.db"
    data_dir = tmp_path / "data"
    store = MemoryStore(database, data_dir)
    store.create_session("session-a", "project-a")
    store.append_message("session-a", "user", "Keep the original SQLite decision in the archive.")
    context = store.add_context_item("session-a", decision("d1", "Use SQLite for the archive."))

    reopened = MemoryStore(database, data_dir)
    restored = reopened.get_context("session-a")
    events = reopened.list_events("session-a")
    projection = data_dir / "contexts" / "session-a" / "context.md"

    assert restored.project_id == "project-a"
    assert restored.context_version == 1
    assert restored.items[0].content == "Use SQLite for the archive."
    assert [event["event_type"] for event in reversed(events)] == [
        "SESSION_CREATED", "MESSAGE_ARCHIVED", "CONTEXT_ITEM_ADDED"
    ]
    assert projection.exists()
    assert "Use SQLite for the archive." in projection.read_text(encoding="utf-8")
    assert "Why: Chosen for the initial implementation." in projection.read_text(encoding="utf-8")


def test_supersession_changes_active_state_but_preserves_archive(tmp_path):
    store = MemoryStore(tmp_path / "db.sqlite", tmp_path / "data")
    store.create_session("s1")
    original = "The first decision was to use SQLite for the archive."
    store.append_message("s1", "user", original)
    store.add_context_item("s1", decision("old", "Use SQLite."))
    context = store.add_context_item("s1", decision("new", "Use PostgreSQL.", supersedes="old"))

    assert next(item for item in context.items if item.id == "old").status == ItemStatus.SUPERSEDED
    assert [item.id for item in context.items if item.type == ItemType.DECISION and item.status == ItemStatus.ACTIVE] == ["new"]
    assert store.search_messages("s1", "first decision SQLite archive")[0]["content"] == original


def test_context_graph_rejects_missing_and_duplicate_supersession(tmp_path):
    store = MemoryStore(tmp_path / "db.sqlite", tmp_path / "data")
    store.create_session("s1")

    with pytest.raises(ContextConflict, match="does not exist"):
        store.add_context_item("s1", decision("d2", "New decision", supersedes="missing"))

    store.add_context_item("s1", decision("d1", "First decision"))
    with pytest.raises(ContextConflict, match="duplicate context item id"):
        store.add_context_item("s1", decision("d1", "Duplicate"))


def test_search_is_scoped_to_session_or_explicit_project(tmp_path):
    store = MemoryStore(tmp_path / "db.sqlite", tmp_path / "data")
    store.create_session("s-a", "project-a")
    store.create_session("s-b", "project-a")
    store.create_session("s-c", "project-c")
    store.append_message("s-a", "user", "The durable archive decision uses SQLite storage.")
    store.append_message("s-b", "assistant", "The durable archive decision uses SQLite storage.")
    store.append_message("s-c", "user", "The durable archive decision uses SQLite storage.")

    assert [row["session_id"] for row in store.search_messages("s-b", "durable archive SQLite")] == ["s-b"]
    project_results = store.search_messages("s-b", "durable archive SQLite", project_id="project-a")
    assert {row["session_id"] for row in project_results} == {"s-a", "s-b"}


def test_sessions_and_project_retrieval_are_isolated_by_tenant(tmp_path):
    store = MemoryStore(tmp_path / "tenant.db", tmp_path / "data")
    alice = store.create_tenant("Alice")
    bob = store.create_tenant("Bob")
    store.create_session("alice-session", "shared-project", alice["id"])
    store.create_session("bob-session", "shared-project", bob["id"])
    store.append_message("alice-session", "user", "Alice secret context datum.", tenant_id=alice["id"])
    store.append_message("bob-session", "user", "Bob unrelated archive entry.", tenant_id=bob["id"])

    assert store.search_messages(
        "bob-session", "Alice secret context", project_id="shared-project", tenant_id=bob["id"]
    ) == []
    with pytest.raises(KeyError):
        store.get_context("alice-session", bob["id"])
    with pytest.raises(KeyError):
        store.append_message("alice-session", "user", "cross tenant", tenant_id=bob["id"])


def test_legacy_v01_database_is_upgraded_and_searchable(tmp_path):
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT, created_at TEXT NOT NULL);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE context_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                version INTEGER NOT NULL, context_json TEXT NOT NULL, created_at TEXT NOT NULL, reason TEXT
            );
            INSERT INTO sessions VALUES ('legacy', 'project', '2026-01-01T00:00:00+00:00');
            INSERT INTO messages(session_id, role, content, created_at)
            VALUES ('legacy', 'user', 'Legacy SQLite archive decision', '2026-01-01T00:00:00+00:00');
        """)

    store = MemoryStore(database, tmp_path / "data")
    result = store.search_messages("legacy", "Legacy SQLite archive")
    assert result and result[0]["retrieval_method"] == "fts5"
    assert result[0]["source"] == "api"
    assert store.get_context("legacy").session_id == "legacy"


def test_lexical_fallback_works_when_fts_table_is_unavailable(tmp_path):
    database = tmp_path / "fallback.sqlite"
    store = MemoryStore(database, tmp_path / "data")
    store.create_session("fallback")
    store.append_message("fallback", "user", "The archive search must fall back to lexical matching.")
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE messages_fts")

    results = store.search_messages("fallback", "archive lexical matching")
    assert results
    assert results[0]["retrieval_method"] == "like"


def test_evidence_window_never_exceeds_requested_limit():
    content = "start " + ("irrelevant words " * 30) + "target " + ("ending words " * 30)
    for limit in (1, 2, 20, 120):
        excerpt = evidence_window(content, "target", limit)
        assert len(excerpt) <= limit
    assert "target" in evidence_window(content, "target", 40)


@pytest.mark.parametrize("max_chars", [64, 128, 256, 12000])
def test_compiler_never_exceeds_budget_and_keeps_request(max_chars):
    context = ActiveContext(
        session_id="s1",
        current_objective="A lengthy objective " * 100,
        items=[decision("d1", "A current durable decision." * 100)],
        open_tasks=["Task " * 100],
    )
    request = "What should happen next?"
    packet = compile_context(context, request, max_chars)
    assert len(packet) <= max_chars
    assert request in packet


def test_context_update_increments_snapshot_version(tmp_path):
    store = MemoryStore(tmp_path / "db.sqlite", tmp_path / "data")
    store.create_session("s1")
    context = store.get_context("s1")
    context.current_objective = "Keep active context bounded."
    updated = store.update_context(context, reason="objective_set")

    assert updated.context_version == 1
    assert "Keep active context bounded." in store.get_context("s1").current_objective
    assert [event["event_type"] for event in store.list_events("s1")] == ["CONTEXT_REPLACED", "SESSION_CREATED"]
