"""Verified SQLite backup and conservative database restore helpers."""

from datetime import UTC, datetime
from pathlib import Path
import os
import sqlite3
import tempfile
from contextlib import closing
from uuid import uuid4


REQUIRED_TABLES = {
    "tenants", "api_keys", "sessions", "messages", "context_events", "context_snapshots"
}


def _validate_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"database file does not exist: {path}")
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {result[0] if result else 'no result'}")
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"not a C-Link database; missing tables: {', '.join(sorted(missing))}")


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Make a consistent SQLite backup, including committed WAL contents."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("backup destination must differ from the live database")
    _validate_database(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_path, timeout=30)) as src, closing(
        sqlite3.connect(destination_path, timeout=30)
    ) as dst:
        src.backup(dst)
    _validate_database(destination_path)
    return destination_path


def restore_database(backup: str | Path, destination: str | Path) -> tuple[Path, Path | None]:
    """Restore a verified backup atomically, preserving a safety copy if possible."""
    backup_path = Path(backup).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if backup_path == destination_path:
        raise ValueError("restore source and database destination are the same file")
    _validate_database(backup_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    safety_copy = None
    if destination_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = destination_path.with_name(
            f"{destination_path.stem}.pre-restore-{stamp}-{uuid4().hex[:8]}"
            f"{destination_path.suffix or '.sqlite3'}"
        )
        backup_database(destination_path, safety_copy)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".restore.tmp", dir=destination_path.parent
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(backup_path, timeout=30)) as src, closing(
            sqlite3.connect(temporary_path, timeout=30)
        ) as dst:
            src.backup(dst)
        _validate_database(temporary_path)
        os.replace(temporary_path, destination_path)
        # WAL/SHM files are transient companions. The service must be stopped
        # during restore so no other process has them open.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(destination_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return destination_path, safety_copy
