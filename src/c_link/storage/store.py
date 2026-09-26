"""Durable session, archive, canonical context, and evidence operations."""

from contextlib import closing
from datetime import UTC, datetime
import json
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from typing import Any
from uuid import uuid4

from c_link.context.graph import ContextGraph
from c_link.context.schema import ActiveContext, ContextItem
from c_link.storage.db import connect, transaction


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not name or name in {".", ".."}:
        raise ValueError("session_id must contain a letter or number")
    return name[:120]


class ContextConflict(ValueError):
    """Raised when a context event conflicts with canonical state."""


class MemoryStore:
    def __init__(self, database: str | Path, data_dir: str | Path | None = None):
        self.database = str(database)
        self.data_dir = Path(data_dir) if data_dir else Path(self.database).parent
        with closing(connect(self.database)):
            pass

    def create_tenant(self, name: str, tenant_id: str | None = None) -> dict[str, Any]:
        tenant_id = tenant_id or f"tn_{uuid4()}"
        now = utc_now()
        with transaction(self.database) as conn:
            conn.execute(
                "INSERT INTO tenants(id, name, active, created_at) VALUES (?, ?, 1, ?)",
                (tenant_id, name, now),
            )
        return {"id": tenant_id, "name": name, "active": True, "created_at": now}

    def list_tenants(self) -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT id, name, active, created_at FROM tenants ORDER BY created_at, id"
            ).fetchall()
        return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def list_api_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT id, name, prefix, created_at, expires_at, revoked_at FROM api_keys "
                "WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def set_tenant_active(self, tenant_id: str, active: bool) -> bool:
        with transaction(self.database) as conn:
            cur = conn.execute("UPDATE tenants SET active = ? WHERE id = ?", (int(active), tenant_id))
        return cur.rowcount == 1

    def delete_tenant(self, tenant_id: str) -> bool:
        if tenant_id == "local":
            raise ValueError("the built-in local tenant cannot be deleted")
        with closing(connect(self.database)) as conn:
            session_rows = conn.execute(
                "SELECT id FROM sessions WHERE tenant_id = ?", (tenant_id,)
            ).fetchall()
        session_ids = [row["id"] for row in session_rows]
        with transaction(self.database) as conn:
            if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
                return False
            conn.execute("DELETE FROM messages WHERE session_id IN "
                         "(SELECT id FROM sessions WHERE tenant_id = ?)", (tenant_id,))
            conn.execute("DELETE FROM context_events WHERE session_id IN "
                         "(SELECT id FROM sessions WHERE tenant_id = ?)", (tenant_id,))
            conn.execute("DELETE FROM context_snapshots WHERE session_id IN "
                         "(SELECT id FROM sessions WHERE tenant_id = ?)", (tenant_id,))
            conn.execute("DELETE FROM sessions WHERE tenant_id = ?", (tenant_id,))
            conn.execute("DELETE FROM api_keys WHERE tenant_id = ?", (tenant_id,))
            conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        if tenant_id != "local":
            tenant_projection = (self.data_dir / "contexts" / "_tenants" / _safe_name(tenant_id)).resolve()
            contexts_root = (self.data_dir / "contexts" / "_tenants").resolve()
            if tenant_projection.is_relative_to(contexts_root) and tenant_projection.exists():
                shutil.rmtree(tenant_projection)
        else:
            for session_id in session_ids:
                projection = (self.data_dir / "contexts" / _safe_name(session_id)).resolve()
                contexts_root = (self.data_dir / "contexts").resolve()
                if projection.is_relative_to(contexts_root) and projection.exists():
                    shutil.rmtree(projection)
        return True

    def issue_api_key(self, tenant_id: str, name: str, expires_at: str | None = None) -> dict[str, str]:
        key = f"cl_live_{secrets.token_urlsafe(32)}"
        prefix = key[:24]
        key_id = str(uuid4())
        now = utc_now()
        with transaction(self.database) as conn:
            tenant = conn.execute(
                "SELECT active FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
            if tenant is None or not tenant["active"]:
                raise KeyError(tenant_id)
            conn.execute(
                "INSERT INTO api_keys(id, tenant_id, name, prefix, key_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_id, tenant_id, name, prefix, hashlib.sha256(key.encode()).hexdigest(), now, expires_at),
            )
        return {"id": key_id, "tenant_id": tenant_id, "name": name, "key": key,
                "prefix": prefix, "created_at": now, "expires_at": expires_at or ""}

    def revoke_api_key(self, prefix: str) -> bool:
        with transaction(self.database) as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE prefix = ? AND revoked_at IS NULL",
                (utc_now(), prefix),
            )
        return cur.rowcount == 1

    def authenticate_api_key(self, key: str) -> str | None:
        if not key.startswith("cl_live_") or len(key) < 40:
            return None
        prefix = key[:24]
        now = utc_now()
        with closing(connect(self.database)) as conn:
            row = conn.execute(
                "SELECT k.key_hash, k.expires_at, t.id AS tenant_id "
                "FROM api_keys k JOIN tenants t ON t.id = k.tenant_id "
                "WHERE k.prefix = ? AND k.revoked_at IS NULL AND t.active = 1",
                (prefix,),
            ).fetchone()
        if row is None or (row["expires_at"] and row["expires_at"] <= now):
            return None
        supplied_hash = hashlib.sha256(key.encode()).hexdigest()
        return row["tenant_id"] if hmac.compare_digest(row["key_hash"], supplied_hash) else None

    def create_session(
        self, session_id: str, project_id: str | None = None, tenant_id: str = "local"
    ) -> ActiveContext:
        now = utc_now()
        context = ActiveContext(session_id=session_id, project_id=project_id)
        payload = context.model_dump_json()
        with transaction(self.database) as conn:
            try:
                conn.execute(
                    "INSERT INTO sessions(id, tenant_id, project_id, created_at, context_json, context_version) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (session_id, tenant_id, project_id, now, payload),
                )
            except sqlite3.IntegrityError as exc:
                raise ContextConflict(f"Session already exists: {session_id}") from exc
            self._event(conn, session_id, "SESSION_CREATED", {"project_id": project_id}, now)
            self._snapshot(conn, context, "session_created", now)
        self._write_projection(context, tenant_id)
        return context

    def list_sessions(self, tenant_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT id, project_id, created_at, context_version FROM sessions "
                "WHERE tenant_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_messages(
        self, session_id: str, tenant_id: str, limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            if conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
            ).fetchone() is None:
                raise KeyError(session_id)
            if before_id is None:
                rows = conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.source "
                    "FROM messages m JOIN sessions s ON s.id = m.session_id "
                    "WHERE m.session_id = ? AND s.tenant_id = ? ORDER BY m.id DESC LIMIT ?",
                    (session_id, tenant_id, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.source "
                    "FROM messages m JOIN sessions s ON s.id = m.session_id "
                    "WHERE m.session_id = ? AND s.tenant_id = ? AND m.id < ? "
                    "ORDER BY m.id DESC LIMIT ?",
                    (session_id, tenant_id, before_id, max(1, min(limit, 500))),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def delete_session(self, session_id: str, tenant_id: str = "local") -> bool:
        with transaction(self.database) as conn:
            if conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
            ).fetchone() is None:
                return False
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM context_events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM context_snapshots WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id))
        projection = self._projection_directory(session_id, tenant_id).resolve()
        contexts_root = (self.data_dir / "contexts").resolve()
        if projection.is_relative_to(contexts_root) and projection.exists():
            shutil.rmtree(projection)
        return True

    def get_context(self, session_id: str, tenant_id: str = "local") -> ActiveContext:
        with closing(connect(self.database)) as conn:
            row = conn.execute(
                "SELECT context_json FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        if not row["context_json"]:
            return ActiveContext(session_id=session_id)
        return ActiveContext.model_validate_json(row["context_json"])

    def update_context(
        self, context: ActiveContext, reason: str = "context_updated", tenant_id: str = "local"
    ) -> ActiveContext:
        now = utc_now()
        with transaction(self.database) as conn:
            row = conn.execute(
                "SELECT context_version FROM sessions WHERE id = ? AND tenant_id = ?",
                (context.session_id, tenant_id),
            ).fetchone()
            if row is None:
                raise KeyError(context.session_id)
            context.context_version = int(row["context_version"]) + 1
            conn.execute(
                "UPDATE sessions SET project_id = ?, context_json = ?, context_version = ? "
                "WHERE id = ? AND tenant_id = ?",
                (context.project_id, context.model_dump_json(), context.context_version, context.session_id, tenant_id),
            )
            self._event(conn, context.session_id, "CONTEXT_REPLACED", context.model_dump(mode="json"), now)
            self._snapshot(conn, context, reason, now)
        self._write_projection(context, tenant_id)
        return context

    def add_context_item(self, session_id: str, item: ContextItem, tenant_id: str = "local") -> ActiveContext:
        now = utc_now()
        with transaction(self.database) as conn:
            row = conn.execute(
                "SELECT context_json, context_version FROM sessions WHERE id = ? AND tenant_id = ?",
                (session_id, tenant_id),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            context = (
                ActiveContext.model_validate_json(row["context_json"])
                if row["context_json"]
                else ActiveContext(session_id=session_id)
            )
            graph = ContextGraph(context)
            try:
                graph.add(item)
            except ValueError as exc:
                raise ContextConflict(str(exc)) from exc
            context.context_version = int(row["context_version"]) + 1
            conn.execute(
                "UPDATE sessions SET context_json = ?, context_version = ? WHERE id = ?",
                (context.model_dump_json(), context.context_version, session_id),
            )
            self._event(conn, session_id, "CONTEXT_ITEM_ADDED", item.model_dump(mode="json"), now)
            self._snapshot(conn, context, f"item_added:{item.type.value.lower()}", now)
        self._write_projection(context, tenant_id)
        return context

    def append_message(
        self, session_id: str, role: str, content: str, source: str = "api", tenant_id: str = "local"
    ) -> dict[str, Any]:
        now = utc_now()
        with transaction(self.database) as conn:
            if conn.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND tenant_id = ?", (session_id, tenant_id)
            ).fetchone() is None:
                raise KeyError(session_id)
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at, source) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, now, source),
            )
            message_id = cur.lastrowid
            self._event(conn, session_id, "MESSAGE_ARCHIVED", {"message_id": message_id, "role": role}, now)
        return {"id": message_id, "session_id": session_id, "role": role, "content": content,
                "created_at": now, "source": source}

    def search_messages(
        self, session_id: str, query: str, limit: int = 5,
        project_id: str | None = None, tenant_id: str = "local",
    ) -> list[dict[str, Any]]:
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms:
            return []
        limit = max(1, min(limit, 20))
        scope_sql = "m.session_id = ? AND s.tenant_id = ?"
        scope_values: list[Any] = [session_id, tenant_id]
        if project_id:
            scope_sql = "s.project_id = ? AND s.tenant_id = ?"
            scope_values = [project_id, tenant_id]
        with closing(connect(self.database)) as conn:
            try:
                match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = conn.execute("""
                    SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.source,
                           bm25(messages_fts) AS score
                    FROM messages_fts f JOIN messages m ON m.id = f.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE messages_fts MATCH ? AND {scope_sql}
                    ORDER BY score, m.id DESC LIMIT ?
                """.format(scope_sql=scope_sql), (match, *scope_values, limit)).fetchall()
                method = "fts5"
            except sqlite3.OperationalError:
                clauses = " AND ".join("lower(content) LIKE ?" for _ in terms)
                params = [f"%{term.lower()}%" for term in terms] + scope_values + [limit]
                rows = conn.execute(f"""
                    SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.source, 0.0 AS score
                    FROM messages m JOIN sessions s ON s.id = m.session_id
                    WHERE {clauses} AND {scope_sql}
                    ORDER BY m.id DESC LIMIT ?
                """, params).fetchall()
                method = "like"
        return [{**dict(row), "retrieval_method": method} for row in rows]

    def list_events(self, session_id: str, limit: int = 100, tenant_id: str = "local") -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT e.id, e.event_type, e.payload_json, e.created_at FROM context_events e "
                "JOIN sessions s ON s.id = e.session_id "
                "WHERE e.session_id = ? AND s.tenant_id = ? ORDER BY e.id DESC LIMIT ?",
                (session_id, tenant_id, max(1, min(limit, 500)))
            ).fetchall()
        return [{"id": row["id"], "event_type": row["event_type"],
                 "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
                for row in rows]

    def list_snapshots(self, session_id: str, limit: int = 100, tenant_id: str = "local") -> list[dict[str, Any]]:
        with closing(connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT c.version, c.created_at, c.reason FROM context_snapshots c "
                "JOIN sessions s ON s.id = c.session_id "
                "WHERE c.session_id = ? AND s.tenant_id = ? ORDER BY c.version DESC LIMIT ?",
                (session_id, tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_snapshot(self, session_id: str, version: int, tenant_id: str = "local") -> ActiveContext:
        with closing(connect(self.database)) as conn:
            row = conn.execute(
                "SELECT c.context_json FROM context_snapshots c JOIN sessions s ON s.id = c.session_id "
                "WHERE c.session_id = ? AND c.version = ? AND s.tenant_id = ?",
                (session_id, version, tenant_id),
            ).fetchone()
        if row is None:
            raise KeyError((session_id, version))
        context = ActiveContext.model_validate_json(row["context_json"])
        return self.update_context(context, reason=f"restored_snapshot:{version}", tenant_id=tenant_id)

    def _event(self, conn: sqlite3.Connection, session_id: str, event_type: str,
               payload: dict[str, Any], created_at: str) -> None:
        conn.execute(
            "INSERT INTO context_events(session_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (session_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
        )

    def _snapshot(self, conn: sqlite3.Connection, context: ActiveContext, reason: str, created_at: str) -> None:
        conn.execute(
            "INSERT INTO context_snapshots(session_id, version, context_json, created_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (context.session_id, context.context_version, context.model_dump_json(), created_at, reason),
        )

    def _write_projection(self, context: ActiveContext, tenant_id: str = "local") -> Path:
        target = self._projection_directory(context.session_id, tenant_id) / "context.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".md.tmp")
        temporary.write_text(render_context(context), encoding="utf-8")
        temporary.replace(target)
        return target

    def _projection_directory(self, session_id: str, tenant_id: str = "local") -> Path:
        if tenant_id == "local":
            return self.data_dir / "contexts" / _safe_name(session_id)
        return self.data_dir / "contexts" / "_tenants" / _safe_name(tenant_id) / _safe_name(session_id)


def render_context(context: ActiveContext) -> str:
    """Render a human-readable portable projection of canonical state."""
    active = [item for item in context.items if item.status.value == "ACTIVE"]
    superseded = [item for item in context.items if item.status.value == "SUPERSEDED"]
    open_questions = [item for item in context.items if item.type.value == "QUESTION"
                      and item.status.value in {"ACTIVE", "OPEN"}]

    def bullet(items: list[ContextItem], include_reason: bool = False) -> str:
        if not items:
            return "- None recorded."
        lines = []
        for item in items:
            line = f"- [{item.id}] {item.content}"
            if include_reason and item.reason:
                line += f" — Why: {item.reason}"
            if item.evidence_refs:
                line += f" (evidence: {', '.join(item.evidence_refs)})"
            lines.append(line)
        return "\n".join(lines)

    decisions = [item for item in active if item.type.value == "DECISION"]
    facts = [item for item in active if item.type.value == "FACT"]
    constraints = [item for item in active if item.type.value == "CONSTRAINT"]
    tasks = [item for item in active if item.type.value == "TASK"]
    open_task_lines = "".join(f"- {task}\n" for task in context.open_tasks)
    completed_lines = "".join(f"- {item}\n" for item in context.completed_work) or "- None recorded.\n"
    result_lines = "".join(f"- {item}\n" for item in context.recent_results) or "- None recorded.\n"
    return f"""# C-Link Active Context

Session: `{context.session_id}`  
Project: `{context.project_id or 'not set'}`  
Context version: `{context.context_version}`

## Current objective
{context.current_objective or 'Not specified.'}

## Current state
{context.current_state or 'Not specified.'}

## Active decisions
{bullet(decisions, include_reason=True)}

## Constraints
{bullet(constraints)}

## Verified facts
{bullet(facts)}

## Open questions
{bullet(open_questions)}

## Current tasks
{bullet(tasks)}
{open_task_lines}

## Completed work
{completed_lines}

## Recent results
{result_lines}

## Superseded decisions and state
{bullet(superseded, include_reason=True)}

## Next step
{context.next_step or 'Not specified.'}
"""


def evidence_window(content: str, query: str, max_chars: int = 1200) -> str:
    """Return a bounded excerpt centered on the first matching query term."""
    if max_chars <= 0:
        return ""
    if len(content) <= max_chars:
        return content
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    lowered = content.casefold()
    index = next((lowered.find(term.casefold()) for term in terms
                  if lowered.find(term.casefold()) >= 0), 0)
    start = max(0, min(index - max_chars // 3, len(content) - max_chars))
    prefix = "…" if start > 0 and max_chars > 1 else ""
    suffix = "…" if start + max_chars - len(prefix) < len(content) and max_chars - len(prefix) > 1 else ""
    body_size = max_chars - len(prefix) - len(suffix)
    excerpt = content[start:start + body_size]
    if start + len(excerpt) < len(content) and max_chars - len(prefix) - len(excerpt) > 0:
        suffix = "…"
    else:
        suffix = ""
    return prefix + excerpt + suffix


def new_session_id() -> str:
    return str(uuid4())
