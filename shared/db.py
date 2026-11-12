"""SQLite connection, migrations and small helpers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Settings, iso

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(settings: Settings) -> sqlite3.Connection:
    settings.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        with conn:
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, iso()))
        applied.append(version)
    return applied


@contextmanager
def tx(conn: sqlite3.Connection):
    """One transaction. Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def row_to_dict(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if k.endswith("_json") and isinstance(v, str):
            try:
                d[k[:-5]] = json.loads(v)
            except ValueError:
                pass
    return d


def rows(conn, sql, params=()) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn, sql, params=()) -> dict | None:
    return row_to_dict(conn.execute(sql, params).fetchone())


def kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
