import sqlite3

import pytest

from c_link.storage.backup import backup_database, restore_database
from c_link.storage.store import MemoryStore


def test_verified_backup_and_restore_create_safety_copy(tmp_path):
    database = tmp_path / "data" / "c_link.sqlite3"
    store = MemoryStore(database, tmp_path / "data")
    store.create_session("backup-test")
    store.append_message("backup-test", "user", "kept in backup")
    backup = backup_database(database, tmp_path / "backups" / "good.sqlite3")
    store.append_message("backup-test", "user", "added after backup")

    restored, safety_copy = restore_database(backup, database)

    assert restored == database.resolve()
    assert safety_copy and safety_copy.is_file()
    reopened = MemoryStore(database, tmp_path / "data")
    assert [item["content"] for item in reopened.search_messages("backup-test", "kept backup")] == [
        "kept in backup"
    ]
    assert reopened.search_messages("backup-test", "added after backup") == []


def test_restore_rejects_non_c_link_sqlite_file_without_changing_target(tmp_path):
    database = tmp_path / "data.sqlite3"
    store = MemoryStore(database)
    store.create_session("preserve")
    bad_backup = tmp_path / "not-c-link.sqlite3"
    with sqlite3.connect(bad_backup) as connection:
        connection.execute("CREATE TABLE arbitrary (value TEXT)")

    with pytest.raises(ValueError, match="not a C-Link database"):
        restore_database(bad_backup, database)

    assert MemoryStore(database).get_context("preserve").session_id == "preserve"
