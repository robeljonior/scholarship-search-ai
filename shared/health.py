"""`tracker health` and backups."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
from datetime import datetime

from . import vault
from .config import Settings, iso, now
from .db import kv_get, one, tx
from .ollama_client import get_llm


def health(conn, settings: Settings) -> dict:
    out: dict = {}
    out["database"] = {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
                       "migrations": [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]}
    llm = get_llm(settings)
    out["ollama"] = {"reachable": llm.available(), "host": settings["ollama_host"],
                     "models_configured": settings["models"],
                     "models_installed": sorted(getattr(llm, "installed", set()))}
    out["vault"] = {"problems": vault.check_permissions(settings), "pin_set": vault.pin_is_set(settings)}
    out["mail"] = {"source": settings["mail_source"], "last_sync": kv_get(conn, "gmail_last_sync"),
                   "token": (settings.vault_dir / "gmail_token.json").exists()}
    out["discovery"] = {
        "sources": conn.execute("SELECT COUNT(*) FROM source").fetchone()[0],
        "failing": conn.execute("SELECT COUNT(*) FROM source WHERE status='failing'").fetchone()[0],
        "pending_review": conn.execute("SELECT COUNT(*) FROM candidate WHERE review_status='needs_user_review'").fetchone()[0],
    }
    jobs = {}
    for name in ("scout", "applicant", "monitor", "run-due"):
        r = one(conn, "SELECT finished_at, outcome FROM job_run WHERE job_name=? ORDER BY id DESC LIMIT 1", (name,))
        jobs[name] = r
    out["jobs"] = jobs
    out["outbox"] = {"undelivered": conn.execute("SELECT COUNT(*) FROM event_outbox WHERE delivered_at IS NULL").fetchone()[0],
                     "failing": conn.execute("SELECT COUNT(*) FROM event_outbox WHERE delivered_at IS NULL AND attempts>0").fetchone()[0]}
    out["approvals_pending"] = conn.execute("SELECT COUNT(*) FROM approval WHERE decision='pending'").fetchone()[0]
    out["telegram_bot"] = bool(settings["telegram"].get("bot_token") and settings["telegram"].get("chat_id"))
    out["backup"] = {"last": kv_get(conn, "last_backup"), "last_restore_test": kv_get(conn, "last_restore_test")}
    out["fetch_mode"] = settings["fetch_mode"]
    return out


def backup(conn, settings: Settings) -> str:
    d = settings.home / "backups"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    stamp = now().strftime("%Y%m%d-%H%M%S")
    db_copy = d / f"tracker-{stamp}.db"
    dst = sqlite3.connect(db_copy)
    conn.backup(dst)
    dst.close()
    archive = d / f"backup-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(db_copy, arcname="tracker.db")
        if settings.vault_dir.exists():
            t.add(settings.vault_dir, arcname="vault")
    db_copy.unlink()
    os.chmod(archive, 0o600)
    with tx(conn):
        from .db import kv_set
        kv_set(conn, "last_backup", {"at": iso(), "path": str(archive)})
    return str(archive)


def restore_test(conn, settings: Settings, archive: str) -> dict:
    tmp = settings.home / "restore-test"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with tarfile.open(archive) as t:
        t.extractall(tmp, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(tmp)  # noqa: S202
    c = sqlite3.connect(tmp / "tracker.db")
    ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = c.execute("SELECT COUNT(*) FROM application").fetchone()[0]
    c.close()
    shutil.rmtree(tmp, ignore_errors=True)
    with tx(conn):
        from .db import kv_set
        kv_set(conn, "last_restore_test", {"at": iso(), "ok": ok, "applications": n})
    return {"ok": ok, "applications": n}
