"""Out-of-band approvals: APPLY (#1), SUBMIT (#2), STATUS, REPLY.

An approval binds a hash of the *current* state of the thing being approved. When the
user decides, the hash is recomputed from the database; if anything changed (deadline,
form values, application status, reply text) the approval is invalidated and a new one
is needed. Approvals are single use, expire, and need the PIN. Agents can create
approvals but no agent command can decide one.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import vault
from .audit import audit, emit
from .config import Settings, iso, now
from .db import one, tx
from .state_machine import transition

TYPES = ("APPLY", "SUBMIT", "STATUS", "REPLY")
MAX_PIN_FAILURES = 5
LOCKOUT_MINUTES = 15


class ApprovalError(Exception):
    pass


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---- current state per type ---------------------------------------------

def current_payload(conn, action_type: str, resource_id: int, stored: dict | None = None) -> dict:
    stored = stored or {}
    if action_type == "APPLY":
        r = one(conn, """SELECT a.id, a.status, o.id AS opportunity_id, o.official_url, o.deadline, o.funding_type,
                         o.amount, o.currency FROM application a JOIN opportunity o ON o.id=a.opportunity_id
                         WHERE a.id=?""", (resource_id,))
        if not r:
            raise ApprovalError("application not found")
        return {"type": "APPLY", "application_id": r["id"], "opportunity_id": r["opportunity_id"],
                "official_url": r["official_url"], "deadline": r["deadline"], "funding_type": r["funding_type"],
                "amount": r["amount"], "currency": r["currency"], "status": r["status"]}
    if action_type == "SUBMIT":
        r = one(conn, """SELECT f.id, f.application_id, f.form_state_hash, p.checksum, p.version, a.status
                         FROM fill_run f JOIN packet p ON p.id=f.packet_id JOIN application a ON a.id=f.application_id
                         WHERE f.id=?""", (resource_id,))
        if not r:
            raise ApprovalError("fill run not found")
        return {"type": "SUBMIT", "fill_run_id": r["id"], "application_id": r["application_id"],
                "form_state_hash": r["form_state_hash"], "packet_checksum": r["checksum"],
                "packet_version": r["version"], "status": r["status"]}
    if action_type == "STATUS":
        r = one(conn, """SELECT e.id, e.application_id, a.status FROM email_record e
                         JOIN application a ON a.id=e.application_id WHERE e.id=?""", (resource_id,))
        if not r:
            raise ApprovalError("email not found or not linked to an application")
        return {"type": "STATUS", "email_id": r["id"], "application_id": r["application_id"],
                "current_status": r["status"], "proposed_status": stored.get("proposed_status")}
    if action_type == "REPLY":
        r = one(conn, "SELECT * FROM reply_draft WHERE id=?", (resource_id,))
        if not r:
            raise ApprovalError("reply draft not found")
        return {"type": "REPLY", "reply_id": r["id"], "recipient": r["recipient"], "subject": r["subject"],
                "body_sha256": hashlib.sha256(r["body"].encode()).hexdigest(), "in_reply_to": r["in_reply_to"],
                "thread_id": r["thread_id"]}
    raise ApprovalError(f"unknown approval type {action_type}")


# ---- create ----------------------------------------------------------------

def create(conn, settings: Settings, action_type: str, resource_id: int, summary: str,
           extra: dict | None = None, hard_deadline: datetime | None = None) -> int:
    if action_type not in TYPES:
        raise ApprovalError(action_type)
    payload = current_payload(conn, action_type, resource_id, extra)
    if extra:
        payload.update(extra)
    ttl = settings["approval_ttl_hours"][action_type]
    expires = now() + timedelta(hours=ttl)
    if hard_deadline and hard_deadline < expires:
        expires = hard_deadline
    with tx(conn):
        conn.execute("""UPDATE approval SET decision='invalidated', decided_at=? WHERE action_type=? AND resource_id=?
                        AND decision='pending'""", (iso(), action_type, resource_id))
        cur = conn.execute(
            """INSERT INTO approval(action_type, resource_id, payload_json, payload_hash, summary, requested_at, expires_at)
               VALUES (?,?,?,?,?,?,?)""",
            (action_type, resource_id, json.dumps(payload, sort_keys=True), payload_hash(payload), summary, iso(),
             iso(expires)))
        aid = cur.lastrowid
        audit(conn, "system", "approval.create", "approval", aid, "ok", {"type": action_type, "resource": resource_id})
        emit(conn, "approval.requested", {"approval_id": aid, "type": action_type, "summary": summary})
    return aid


# ---- decide ------------------------------------------------------------------

def _recent_pin_failures(conn) -> int:
    since = iso(now() - timedelta(minutes=LOCKOUT_MINUTES))
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='pin_failed' AND ts>=?", (since,)).fetchone()[0]


def decide(conn, settings: Settings, approval_id: int, approve: bool, pin: str, via: str,
           chat_id: str | None = None) -> dict:
    """Verify everything, then apply the decision in one transaction.

    via: 'terminal' | 'telegram' | 'app-dev'. Telegram also requires the configured chat id.
    """
    if via == "telegram":
        expected = str(settings["telegram"].get("chat_id") or "")
        if not expected or str(chat_id) != expected:
            with tx(conn):
                audit(conn, f"telegram:{chat_id}", "approval.decide", "approval", approval_id, "denied", "wrong chat")
            raise ApprovalError("approvals are only accepted from the configured Telegram chat")
    if _recent_pin_failures(conn) >= MAX_PIN_FAILURES:
        raise ApprovalError(f"too many wrong PINs; locked for {LOCKOUT_MINUTES} minutes")
    if not vault.check_pin(settings, pin):
        with tx(conn):
            audit(conn, via, "pin_failed", "approval", approval_id, "denied")
        raise ApprovalError("wrong PIN" if vault.pin_is_set(settings) else "no PIN set; run `tracker approvals set-pin`")

    after_commit = None
    with tx(conn):
        a = one(conn, "SELECT * FROM approval WHERE id=?", (approval_id,))
        if not a:
            raise ApprovalError("approval not found")
        if a["decision"] != "pending" or a["consumed_at"]:
            raise ApprovalError(f"approval already {a['decision']}")
        if datetime.fromisoformat(a["expires_at"]) <= now():
            conn.execute("UPDATE approval SET decision='expired', decided_at=? WHERE id=?", (iso(), approval_id))
            _on_expired(conn, a)
            audit(conn, via, "approval.decide", "approval", approval_id, "expired")
            return {"approval_id": approval_id, "result": "expired"}
        stored = json.loads(a["payload_json"])
        current = current_payload(conn, a["action_type"], a["resource_id"], stored)
        for k in ("proposed_status",):
            if k in stored:
                current[k] = stored[k]
        if payload_hash(current) != a["payload_hash"]:
            conn.execute("UPDATE approval SET decision='invalidated', decided_at=? WHERE id=?", (iso(), approval_id))
            audit(conn, via, "approval.decide", "approval", approval_id, "invalidated", "payload changed")
            emit(conn, "approval.invalidated", {"approval_id": approval_id, "type": a["action_type"]})
            return {"approval_id": approval_id, "result": "invalidated",
                    "message": "what you were approving changed since the request; a new approval is needed"}
        decision = "approved" if approve else "rejected"
        conn.execute("UPDATE approval SET decision=?, decided_at=?, decided_via=?, consumed_at=? WHERE id=?",
                     (decision, iso(), via, iso(), approval_id))
        after_commit = _apply(conn, settings, a, stored, approve)
        audit(conn, via, "approval.decide", "approval", approval_id, decision, {"type": a["action_type"]})
        emit(conn, "approval.decided", {"approval_id": approval_id, "type": a["action_type"], "decision": decision})
    result = {"approval_id": approval_id, "result": "approved" if approve else "rejected", "type": a["action_type"]}
    if after_commit:
        result.update(after_commit() or {})
    return result


def _apply(conn, settings, a: dict, payload: dict, approve: bool):
    t, rid = a["action_type"], a["resource_id"]
    actor = f"approval:{t}"
    if t == "APPLY":
        if approve:
            transition(conn, rid, "PREPARING", actor, "approval #1 granted", f"approval:{a['id']}")
        else:
            transition(conn, rid, "SKIPPED", "user", "approval #1 rejected", f"approval:{a['id']}")
        return None
    if t == "SUBMIT":
        app_id = payload["application_id"]
        if approve:
            from validateagain_applay.prepare import placeholders
            run = conn.execute("SELECT form_values_json FROM fill_run WHERE id=?", (rid,)).fetchone()
            values = json.loads(run["form_values_json"] or "{}") if run else {}
            left = [p for v in values.values() for p in placeholders(str(v))]
            if left:
                raise ApprovalError(f"the filled form still contains unfinished placeholders ({left[0][:60]}); "
                                    "edit it in the portal first")
            transition(conn, app_id, "SUBMITTING", actor, "approval #2 granted", f"approval:{a['id']}")
        else:
            transition(conn, app_id, "READY_TO_FILL", "user", "approval #2 rejected; refill needed", f"approval:{a['id']}")
        return None
    if t == "STATUS":
        conn.execute("UPDATE email_record SET handled_at=? WHERE id=?", (iso(), rid))
        if approve:
            transition(conn, payload["application_id"], payload["proposed_status"], actor,
                       "status confirmed from email", f"email:{rid}")
        return None
    if t == "REPLY":
        if not approve:
            conn.execute("UPDATE reply_draft SET status='rejected' WHERE id=?", (rid,))
            return None
        # Consumed already (above). Mark sending inside the tx; the send happens once, after commit.
        conn.execute("UPDATE reply_draft SET status='sending' WHERE id=?", (rid,))

        def send():
            from track_response.replies import send_approved_reply
            return send_approved_reply(conn, settings, rid)
        return send
    raise ApprovalError(t)


def _on_expired(conn, a: dict) -> None:
    t = a["action_type"]
    payload = json.loads(a["payload_json"])
    app_id = payload.get("application_id")
    status = conn.execute("SELECT status FROM application WHERE id=?", (app_id,)).fetchone() if app_id else None
    if t == "APPLY" and status and status["status"] == "AWAITING_APPLY":
        transition(conn, app_id, "SHORTLISTED", "system", "approval #1 expired")
    elif t == "SUBMIT" and status and status["status"] == "AWAITING_SUBMIT":
        transition(conn, app_id, "READY_TO_FILL", "system", "approval #2 expired")
    elif t == "REPLY":
        conn.execute("UPDATE reply_draft SET status='rejected', error='approval expired' WHERE id=?", (a["resource_id"],))
    emit(conn, "approval.expired", {"approval_id": a["id"], "type": t})


def expire_due(conn) -> int:
    n = 0
    with tx(conn):
        for a in conn.execute("SELECT * FROM approval WHERE decision='pending' AND expires_at<=?", (iso(),)).fetchall():
            conn.execute("UPDATE approval SET decision='expired', decided_at=? WHERE id=?", (iso(), a["id"]))
            _on_expired(conn, dict(a))
            n += 1
    return n


def pending(conn) -> list[dict]:
    from .db import rows
    return rows(conn, "SELECT * FROM approval WHERE decision='pending' ORDER BY requested_at")
