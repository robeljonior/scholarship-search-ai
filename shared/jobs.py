"""Job runs, outbox dispatch and the `run-due` recovery entry point."""
from __future__ import annotations

import json
import traceback
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import Settings, iso, now
from .db import tx


@contextmanager
def job(conn, name: str):
    with tx(conn):
        cur = conn.execute("INSERT INTO job_run(job_name, started_at, outcome) VALUES (?,?, 'running')", (name, iso()))
    run_id = cur.lastrowid
    summary: dict = {}
    try:
        yield summary
    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        summary["trace"] = traceback.format_exc(limit=5)
        with tx(conn):
            conn.execute("UPDATE job_run SET finished_at=?, outcome='failed', summary_json=? WHERE id=?",
                         (iso(), json.dumps(summary, default=str), run_id))
        raise
    with tx(conn):
        conn.execute("UPDATE job_run SET finished_at=?, outcome='ok', summary_json=? WHERE id=?",
                     (iso(), json.dumps(summary, default=str), run_id))


def last_success(conn, name: str) -> datetime | None:
    r = conn.execute("SELECT MAX(finished_at) FROM job_run WHERE job_name=? AND outcome='ok'", (name,)).fetchone()
    return datetime.fromisoformat(r[0]) if r and r[0] else None


def is_due(conn, name: str, every: timedelta) -> bool:
    last = last_success(conn, name)
    return last is None or now() - last >= every


# ---- outbox dispatch ------------------------------------------------------

def _telegram_send(settings: Settings, text: str) -> None:
    tg = settings["telegram"]
    if not tg.get("bot_token") or not tg.get("chat_id"):
        raise RuntimeError("telegram not configured")
    data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text[:4000]}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", data=data, timeout=15):
        pass


def format_event(event_type: str, payload: dict) -> str:
    if event_type == "application.status":
        return f"Application #{payload['application_id']}: {payload['old']} -> {payload['new']}. {payload.get('reason', '')}"
    if event_type == "approval.requested":
        return f"Approval needed ({payload['type']} #{payload['approval_id']}):\n{payload['summary']}"
    if event_type == "reminder":
        return payload.get("text", "reminder")
    return f"{event_type}: {json.dumps(payload, default=str)[:500]}"


def dispatch(conn, settings: Settings, limit: int = 50) -> dict:
    """Deliver undelivered events: Telegram if configured, always the local notifications log."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log = settings.logs_dir / "notifications.log"
    sent = failed = 0
    due = conn.execute("""SELECT * FROM event_outbox WHERE delivered_at IS NULL AND attempts < 8
                          AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?""",
                       (iso(), limit)).fetchall()
    use_tg = bool(settings["telegram"].get("bot_token") and settings["telegram"].get("chat_id"))
    for ev in due:
        text = format_event(ev["event_type"], json.loads(ev["payload_json"]))
        try:
            # approval requests go through the approval bot itself (with buttons), not here
            if use_tg and ev["event_type"] in ("application.status", "reminder", "approval.expired"):
                _telegram_send(settings, text)
            with open(log, "a") as f:
                f.write(f"{iso()} [{ev['event_type']}] {text}\n")
            with tx(conn):
                conn.execute("UPDATE event_outbox SET delivered_at=?, attempts=attempts+1 WHERE id=?", (iso(), ev["id"]))
            sent += 1
        except Exception as e:  # noqa: BLE001 - every failure is recorded and retried
            backoff = timedelta(minutes=2 ** min(ev["attempts"], 8))
            with tx(conn):
                conn.execute("UPDATE event_outbox SET attempts=attempts+1, last_error=?, next_attempt_at=? WHERE id=?",
                             (str(e)[:500], iso(now() + backoff), ev["id"]))
            failed += 1
    return {"delivered": sent, "failed": failed}


def run_due(conn, settings: Settings) -> dict:
    """Recovery-safe daily entry point (systemd timer). Runs whatever is due."""
    from . import approvals
    from search_find_validate.agent import run_scout
    from validateagain_applay.agent import run_applicant
    from track_response.agent import run_monitor

    out: dict = {"expired_approvals": approvals.expire_due(conn)}
    out["scout"] = run_scout(conn, settings, only_due=True)
    if is_due(conn, "applicant", timedelta(hours=20)):
        out["applicant"] = run_applicant(conn, settings)
    if is_due(conn, "monitor", timedelta(hours=4)):
        out["monitor"] = run_monitor(conn, settings)
    out["dispatch"] = dispatch(conn, settings)
    return out
