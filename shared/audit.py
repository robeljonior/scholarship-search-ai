"""Audit log and event outbox writers. Both are written inside the caller's transaction."""
from __future__ import annotations

import json

from .config import iso


def audit(conn, actor: str, action: str, resource_type: str = "", resource_id="", outcome: str = "ok",
          detail: str | dict = "") -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, default=str)
    conn.execute(
        "INSERT INTO audit_log(actor, action, resource_type, resource_id, outcome, detail, ts) VALUES (?,?,?,?,?,?,?)",
        (actor, action, resource_type, str(resource_id), outcome, detail, iso()),
    )


def emit(conn, event_type: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO event_outbox(event_type, payload_json, created_at, next_attempt_at) VALUES (?,?,?,?)",
        (event_type, json.dumps(payload, default=str), iso(), iso()),
    )
    return cur.lastrowid
