"""SQLite setup and schema management for the C-Link memory store."""

from pathlib import Path
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from threading import Lock


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    prefix TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'local' REFERENCES tenants(id),
    project_id TEXT,
    created_at TEXT NOT NULL,
    context_json TEXT,
    context_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api'
);

CREATE INDEX IF NOT EXISTS idx_messages_session_time
    ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS context_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_time
    ON context_events(session_id, id);

CREATE TABLE IF NOT EXISTS context_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version INTEGER NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT,
    UNIQUE(session_id, version)
);
"""

_INITIALIZED_DATABASES: set[str] = set()
_INITIALIZATION_LOCK = Lock()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a configured connection; initialize each on-disk database once."""
    db_path = str(path)
    is_memory = db_path == ":memory:"
    if not is_memory:
        resolved = Path(db_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        cache_key = str(resolved).casefold()

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if is_memory:
        try:
            _initialize(conn, db_path)
        except Exception:
            conn.close()
            raise
    elif cache_key not in _INITIALIZED_DATABASES:
        with _INITIALIZATION_LOCK:
            if cache_key not in _INITIALIZED_DATABASES:
                try:
                    _initialize(conn, db_path)
                    _INITIALIZED_DATABASES.add(cache_key)
                except Exception:
                    conn.close()
                    raise
    return conn


def _initialize(conn: sqlite3.Connection, db_path: str) -> None:
    """Create or upgrade schema, indexes, and optional FTS5 structures."""
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    from datetime import UTC, datetime
    conn.execute(
        "INSERT OR IGNORE INTO tenants(id, name, active, created_at) VALUES ('local', 'Local workspace', 1, ?)",
        (datetime.now(UTC).isoformat(),),
    )

    # Upgrade databases created by the original v0.1 skeleton in place.
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "context_json" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN context_json TEXT")
    if "context_version" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN context_version INTEGER NOT NULL DEFAULT 0")
    if "tenant_id" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant_project ON sessions(tenant_id, project_id)")

    message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "source" not in message_columns:
        conn.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'api'")

    fts_was_present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages_fts'"
    ).fetchone() is not None
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
            "USING fts5(content, session_id UNINDEXED, tokenize='unicode61')"
        )
        conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content, session_id)
            VALUES (new.id, new.content, new.session_id);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF content, session_id ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.id;
            INSERT INTO messages_fts(rowid, content, session_id)
            VALUES (new.id, new.content, new.session_id);
        END;
        """)
        if not fts_was_present:
            conn.execute("""
                INSERT INTO messages_fts(rowid, content, session_id)
                SELECT m.id, m.content, m.session_id FROM messages m
            """)
    except sqlite3.OperationalError:
        pass

    conn.commit()


@contextmanager
def transaction(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Run a write unit atomically and always close its connection."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
