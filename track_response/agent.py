"""AGENT 3 - MONITOR. Step 8 TRACK, plus reminders.

- submission check: every SUBMITTED application has a saved confirmation; confirmation email seen?
- mail sync -> classify -> link to application -> STATUS approval / REPLY draft + approval
- deadline reminders (60/30/14/7/1 days), idempotent, with missed-run catch-up
The classifier never writes a status. Only a consumed STATUS approval does.
"""
from __future__ import annotations

import email.utils
import json
import re
from datetime import date, datetime, timezone

from shared import approvals
from shared.audit import audit, emit
from shared.config import Settings, iso, now
from shared.db import one, rows, tx
from shared.jobs import job
from shared.ollama_client import get_llm
from shared.state_machine import POST_SUBMIT, TERMINAL, TRANSITIONS

from . import replies
from .classify import PROPOSED_STATUS, classify
from .mailbox import MailError, get_mailbox, sender_address


def _domain(addr: str) -> str:
    d = addr.split("@")[-1].lower() if "@" in addr else ""
    return d[4:] if d.startswith("www.") else d


def _base(d: str) -> str:
    parts = d.split(".")
    if len(parts) >= 3 and parts[-2] in ("ac", "edu", "gov", "co", "org"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def link_application(conn, from_domain: str, subject: str, body: str) -> int | None:
    for a in rows(conn, """SELECT a.id, a.reference_number, o.official_domain, o.name FROM application a
                           JOIN opportunity o ON o.id=a.opportunity_id"""):
        if a["reference_number"] and a["reference_number"] in f"{subject} {body}":
            return a["id"]
    for a in rows(conn, """SELECT a.id, o.official_domain, o.name FROM application a JOIN opportunity o
                           ON o.id=a.opportunity_id WHERE a.status NOT IN ('DISCOVERED','BELOW_THRESHOLD','INELIGIBLE')
                           ORDER BY CASE WHEN a.status IN ('SUBMITTED','UNDER_REVIEW','INTERVIEW','WAITING_FOR_DOCUMENTS',
                           'ADMITTED') THEN 0 ELSE 1 END, a.id"""):
        if from_domain and _base(from_domain) == _base(a["official_domain"]):
            return a["id"]
    return None


def process_message(conn, settings: Settings, m: dict, llm) -> dict:
    sender = sender_address(m.get("from", ""))
    reply_to = sender_address(m.get("reply_to", "")) if m.get("reply_to") else ""
    fdom = _domain(sender)
    suspicious = int(bool(reply_to) and _base(_domain(reply_to)) != _base(fdom))
    body = m.get("body", "")  # in memory only
    cat, conf, how = classify(m.get("subject", ""), body, llm)
    app_id = link_application(conn, fdom, m.get("subject", ""), body)
    try:
        received = email.utils.parsedate_to_datetime(m.get("date")).astimezone(timezone.utc).isoformat() if m.get("date") else None
    except (TypeError, ValueError):
        received = None
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO email_record(gmail_id, thread_id, message_id_header, received_at, from_addr, from_domain, reply_to,
               subject, category, confidence, application_id, suspicious, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(gmail_id) DO NOTHING""",
            (m["id"], m.get("threadId"), m.get("message_id"), received, sender, fdom, reply_to, m.get("subject"), cat,
             conf, app_id, suspicious, iso()))
        if cur.rowcount == 0:
            return {"skipped": True}
        eid = cur.lastrowid
        audit(conn, "monitor", "email.classified", "email", eid, "ok", {"category": cat, "how": how, "app": app_id})
    result = {"email_id": eid, "category": cat, "application_id": app_id}
    if not app_id:
        return result
    app = one(conn, "SELECT status FROM application WHERE id=?", (app_id,))
    if cat == "confirmation":
        with tx(conn):
            conn.execute("""INSERT INTO evidence(application_id, kind, note, created_at) VALUES (?,?,?,?)""",
                         (app_id, "email", f"confirmation email {eid}: {m.get('subject')}", iso()))
            conn.execute("UPDATE email_record SET handled_at=? WHERE id=?", (iso(), eid))
    elif cat in PROPOSED_STATUS:
        proposed = PROPOSED_STATUS[cat]
        if app["status"] in POST_SUBMIT and proposed in TRANSITIONS.get(app["status"], set()) and not suspicious:
            approvals.create(conn, settings, "STATUS", eid,
                             f"STATUS #{app_id}: email from {sender} suggests {proposed} (now {app['status']}).\n"
                             f"Subject: {m.get('subject')}\nRead the email in Gmail before approving.",
                             extra={"proposed_status": proposed})
            result["status_proposal"] = proposed
        elif suspicious:
            with tx(conn):
                emit(conn, "email.suspicious", {"email_id": eid, "from": sender, "reply_to": reply_to})
        if cat in ("interview", "docs_requested"):
            result["reply_draft"] = replies.draft(conn, settings, eid, cat, body, llm)
    return result


def submission_check(conn) -> list[dict]:
    out = []
    for a in rows(conn, "SELECT id, updated_at FROM application WHERE status='SUBMITTED'"):
        conf = one(conn, "SELECT id FROM evidence WHERE application_id=? AND kind='confirmation'", (a["id"],))
        mail = one(conn, "SELECT id FROM evidence WHERE application_id=? AND kind='email'", (a["id"],))
        issue = None
        if not conf:
            issue = "no confirmation page saved"
        elif not mail and (now() - datetime.fromisoformat(a["updated_at"])).total_seconds() > 48 * 3600:
            issue = "no confirmation email after 48 hours; check the portal"
        if issue:
            with tx(conn):
                conn.execute("""INSERT INTO task(application_id, kind, title, created_at) VALUES (?,?,?,?)
                                ON CONFLICT(application_id, title) DO NOTHING""", (a["id"], "portal", issue, iso()))
            out.append({"application_id": a["id"], "issue": issue})
    return out


def reminders(conn, settings: Settings) -> int:
    sent = 0
    thresholds = sorted(settings["reminder_days"], reverse=True)
    active = rows(conn, """SELECT a.id, a.status, o.name, o.deadline FROM application a JOIN opportunity o
                           ON o.id=a.opportunity_id WHERE o.deadline IS NOT NULL AND a.status IN
                           ('SHORTLISTED','AWAITING_APPLY','PREPARING','READY_TO_FILL','FILLING','NEEDS_USER',
                            'AWAITING_SUBMIT','SUBMITTING')""")
    for a in active:
        try:
            days = (date.fromisoformat(a["deadline"][:10]) - now().date()).days
        except ValueError:
            continue
        # catch-up: if several thresholds were missed while the laptop was off, send only the nearest one
        due = [t for t in thresholds if days <= t]
        if days < 0:
            due = [0]
        if not due:
            continue
        t = min(due)
        if one(conn, "SELECT 1 AS x FROM reminder_sent WHERE application_id=? AND threshold_days=?", (a["id"], t)):
            continue
        open_tasks = conn.execute("SELECT COUNT(*) FROM task WHERE application_id=? AND status NOT IN ('DONE','CANCELLED')",
                                  (a["id"],)).fetchone()[0]
        text = (f"Deadline in {days} days: #{a['id']} {a['name']} ({a['deadline']}). Status {a['status']}, "
                f"{open_tasks} open tasks." if days >= 0 else
                f"OVERDUE: #{a['id']} {a['name']} deadline {a['deadline']} passed while status is {a['status']}.")
        with tx(conn):
            conn.execute("INSERT INTO reminder_sent VALUES (?,?,?)", (a["id"], t, iso()))
            for skipped in (x for x in thresholds if x > t):
                conn.execute("INSERT OR IGNORE INTO reminder_sent VALUES (?,?,?)", (a["id"], skipped, iso()))
            emit(conn, "reminder", {"application_id": a["id"], "days": days, "text": text})
        sent += 1
    return sent


def run_monitor(conn, settings: Settings, mailbox=None, llm=None) -> dict:
    with job(conn, "monitor") as summary:
        llm = llm if llm is not None else get_llm(settings)
        summary["submission_issues"] = submission_check(conn)
        try:
            mb = mailbox or get_mailbox(settings)
            msgs = mb.fetch_new(conn)
            results = [process_message(conn, settings, m, llm) for m in msgs]
            results = [r for r in results if not r.get("skipped")]
            summary["emails_new"] = len(results)
            summary["status_proposals"] = sum(1 for r in results if r.get("status_proposal"))
            summary["reply_drafts"] = sum(1 for r in results if r.get("reply_draft"))
            summary["by_category"] = {c: sum(1 for r in results if r["category"] == c)
                                      for c in {r["category"] for r in results}}
        except MailError as e:
            summary["mail_error"] = str(e)
        summary["reminders_sent"] = reminders(conn, settings)
        summary["expired_approvals"] = approvals.expire_due(conn)
    return summary
