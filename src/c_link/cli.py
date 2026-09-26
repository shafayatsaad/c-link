"""Local command-line entry point for the C-Link memory gateway."""

import json
import ipaddress
import os
from pathlib import Path
import sqlite3
import typer

from c_link.api.app import app as api_app, get_store
from c_link.storage.store import ContextConflict
from c_link.storage.backup import backup_database, restore_database
from c_link.providers.llamacpp import ProviderError, list_models, provider_base_url


app = typer.Typer(help="C-Link local context and memory gateway.")
session_app = typer.Typer(help="Manage memory sessions.")
tenant_app = typer.Typer(help="Provision isolated tenant accounts and API keys.")
app.add_typer(session_app, name="session")
app.add_typer(tenant_app, name="tenant")


def _database_path() -> Path:
    data_dir = Path(os.environ.get("C_LINK_DATA_DIR", "data")).expanduser()
    return Path(os.environ.get("C_LINK_DATABASE", str(data_dir / "c_link.db"))).expanduser()


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@app.command()
def version():
    from c_link import __version__
    typer.echo(f"C-Link {__version__}")


@app.command()
def run(host: str = "127.0.0.1", port: int = 9931):
    """Start the local HTTP API."""
    if not _is_loopback(host) and (
        os.environ.get("C_LINK_ENV", "").casefold() != "production"
        or
        os.environ.get("C_LINK_AUTH_MODE", "").casefold() != "api_key"
        or os.environ.get("C_LINK_BEHIND_TLS_PROXY", "").casefold() not in {"1", "true", "yes"}
    ):
        raise typer.BadParameter(
            "non-loopback binding requires C_LINK_AUTH_MODE=api_key and a TLS-terminating reverse proxy"
        )
    import uvicorn
    uvicorn.run(api_app, host=host, port=port)


@app.command()
def doctor(require_model: bool = False):
    """Check database integrity and optionally require a reachable model provider."""
    database = _database_path()
    ok = True
    try:
        store = get_store()
        with sqlite3.connect(database) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        typer.echo(f"Database: OK ({database.resolve()}) — {integrity}")
        typer.echo(f"C-Link package: ready ({store.__class__.__name__})")
    except Exception as exc:
        ok = False
        typer.echo(f"Database: FAILED — {exc}", err=True)

    try:
        models = list_models().get("data", [])
        typer.echo(f"Model provider: OK ({provider_base_url()}; {len(models)} model(s))")
    except ProviderError as exc:
        typer.echo(f"Model provider: unavailable ({exc})")
        if require_model:
            ok = False
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def backup(output: Path | None = None):
    """Create and verify a consistent backup of the configured SQLite database."""
    if output is None:
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path("backups") / f"c-link-{stamp}.sqlite3"
    try:
        result = backup_database(_database_path(), output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Backup failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Verified backup created: {result}")


@app.command()
def restore(backup_file: Path):
    """Restore a verified C-Link backup after stopping the gateway."""
    try:
        destination, safety_copy = restore_database(backup_file, _database_path())
    except (OSError, ValueError, sqlite3.Error) as exc:
        typer.echo(f"Restore failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Restored database: {destination}")
    if safety_copy:
        typer.echo(f"Previous database saved as: {safety_copy}")


@session_app.command("create")
def create_session(session_id: str | None = None, project_id: str | None = None):
    """Create a durable memory session."""
    store = get_store()
    if session_id is None:
        from c_link.storage.store import new_session_id
        session_id = new_session_id()
    try:
        context = store.create_session(session_id, project_id)
    except ContextConflict as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({"session_id": session_id, "context": context.model_dump(mode="json")}, indent=2))


@app.command()
def message(session_id: str, role: str, content: str):
    """Archive one message without sending it to a model."""
    if role not in {"system", "user", "assistant", "tool"}:
        raise typer.BadParameter("role must be system, user, assistant, or tool")
    try:
        result = get_store().append_message(session_id, role, content, source="cli")
    except KeyError as exc:
        raise typer.BadParameter("session not found") from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def search(session_id: str, query: str, limit: int = 5, project_id: str | None = None):
    """Search archived messages using SQLite FTS5 or its lexical fallback."""
    results = get_store().search_messages(session_id, query, limit, project_id)
    typer.echo(json.dumps(results, ensure_ascii=False, indent=2))


@tenant_app.command("create")
def create_tenant(name: str):
    """Create a tenant and print its first API key once."""
    store = get_store()
    tenant = store.create_tenant(name)
    key = store.issue_api_key(tenant["id"], "initial")
    typer.echo(json.dumps({"tenant": tenant, "api_key": key["key"]}, ensure_ascii=False, indent=2))
    typer.echo("Save this API key now; C-Link does not store the plaintext key.", err=True)


@tenant_app.command("list")
def list_tenants():
    """List tenant IDs and status (never API key material)."""
    typer.echo(json.dumps(get_store().list_tenants(), ensure_ascii=False, indent=2))


@tenant_app.command("keys")
def list_tenant_keys(tenant_id: str):
    """List a tenant's key metadata; secret values are never returned."""
    typer.echo(json.dumps(get_store().list_api_keys(tenant_id), ensure_ascii=False, indent=2))


@tenant_app.command("key")
def create_tenant_key(tenant_id: str, name: str):
    """Issue another API key for a tenant; keys share that tenant's data."""
    try:
        result = get_store().issue_api_key(tenant_id, name)
    except KeyError as exc:
        raise typer.BadParameter("active tenant not found") from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    typer.echo("Save this API key now; C-Link does not store the plaintext key.", err=True)


@tenant_app.command("revoke-key")
def revoke_tenant_key(prefix: str):
    """Revoke an API key using the public key prefix."""
    if not get_store().revoke_api_key(prefix):
        raise typer.BadParameter("active API key prefix not found")
    typer.echo("API key revoked.")


@tenant_app.command("suspend")
def suspend_tenant(tenant_id: str):
    """Disable all API keys for a tenant without deleting its stored data."""
    if not get_store().set_tenant_active(tenant_id, False):
        raise typer.BadParameter("tenant not found")
    typer.echo("Tenant suspended.")


@tenant_app.command("resume")
def resume_tenant(tenant_id: str):
    """Re-enable keys for a suspended tenant."""
    if not get_store().set_tenant_active(tenant_id, True):
        raise typer.BadParameter("tenant not found")
    typer.echo("Tenant active.")


@tenant_app.command("delete")
def delete_tenant(tenant_id: str, confirm: bool = False):
    """Permanently delete a tenant, its API keys, sessions, messages, and snapshots."""
    if not confirm:
        typer.confirm(
            f"Permanently erase all C-Link data owned by tenant {tenant_id}?",
            abort=True,
        )
    try:
        deleted = get_store().delete_tenant(tenant_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not deleted:
        raise typer.BadParameter("tenant not found")
    typer.echo("Tenant data erased.")
