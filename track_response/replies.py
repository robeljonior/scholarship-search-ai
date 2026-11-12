"""REPLY drafts and sending. One approval sends at most one email; sending never retries."""
from __future__ import annotations

import hashlib
import re

from shared import approvals, vault
from shared.audit import audit
from shared.config import Settings, iso
from shared.db import one, tx
from shared.ollama_client import LLMError

MAX_CHARS = 2000
BANNED = r"\b(payment|pay|bank|iban|swift|card|credit card|password|passcode|pin|wire transfer|western union|bitcoin)\b"

TEMPLATES = {
    "interview": ("Dear {name},\n\nThank you for inviting me to an interview for {program}. I am available and would "
                  "be glad to attend at the proposed time. If another slot is more convenient, please let me know.\n\n"
                  "Kind regards,\n{me}"),
    "docs_requested": ("Dear {name},\n\nThank you for your message about my application to {program}. I will upload "
                       "the requested documents to the application portal as soon as possible and will confirm once "
                       "this is done.\n\nKind regards,\n{me}"),
}


def check_text(body: str) -> list[str]:
    problems = []
    if len(body) > MAX_CHARS:
        problems.append("too long")
    if re.search(r"https?://|www\.", body, re.I):
        problems.append("contains a link")
    if re.search(BANNED, body, re.I):
        problems.append("mentions payment/bank/password")
    return problems


def draft(conn, settings: Settings, email_id: int, category: str, body_in_memory: str, llm=None) -> int | None:
    e = one(conn, "SELECT * FROM email_record WHERE id=?", (email_id,))
    if category not in TEMPLATES or not e:
        return None
    profile = vault.load_profile(settings)
    prog = one(conn, """SELECT o.name FROM application a JOIN opportunity o ON o.id=a.opportunity_id WHERE a.id=?""",
               (e["application_id"],)) if e["application_id"] else None
    name = (e["from_addr"] or "").split("@")[0].replace(".", " ").title() or "Sir or Madam"
    body = TEMPLATES[category].format(name="Admissions Team" if "@" in (e["from_addr"] or "") else name,
                                      program=prog["name"] if prog else "the programme", me=profile.get("full_name"))
    if llm is not None and llm.available():
        try:
            out = llm.generate_text("draft", "Write a short, polite reply to this email. No links, no payment or "
                                    "bank details, no promises you cannot keep. Sign with the applicant's name.",
                                    f"APPLICANT: {profile.get('full_name')}\nEMAIL SUBJECT: {e['subject']}\n"
                                    f"EMAIL: {body_in_memory[:3000]}", max_chars=MAX_CHARS + 500)
            if out and not check_text(out):
                body = out
        except LLMError:
            pass
    if check_text(body):
        return None
    recipient = e["from_addr"]
    subject = e["subject"] if (e["subject"] or "").lower().startswith("re:") else f"Re: {e['subject']}"
    with tx(conn):
        cur = conn.execute("""INSERT INTO reply_draft(email_record_id, recipient, subject, body, body_sha256, in_reply_to,
                              thread_id, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                           (email_id, recipient, subject, body, hashlib.sha256(body.encode()).hexdigest(),
                            e["message_id_header"], e["thread_id"], iso()))
        rid = cur.lastrowid
    warn = ""
    if e["suspicious"]:
        warn = "\nWARNING: Reply-To differs from the sender's domain."
    approvals.create(conn, settings, "REPLY", rid, f"REPLY to {recipient}\nSubject: {subject}{warn}\n\n{body}")
    return rid


def edit_draft(conn, settings: Settings, reply_id: int, new_body: str) -> int:
    """User-only. A new text needs a new approval."""
    problems = check_text(new_body)
    if problems:
        raise ValueError(", ".join(problems))
    with tx(conn):
        conn.execute("UPDATE reply_draft SET body=?, body_sha256=?, status='awaiting_approval' WHERE id=?",
                     (new_body, hashlib.sha256(new_body.encode()).hexdigest(), reply_id))
    r = one(conn, "SELECT * FROM reply_draft WHERE id=?", (reply_id,))
    return approvals.create(conn, settings, "REPLY", reply_id, f"REPLY to {r['recipient']}\nSubject: {r['subject']}\n\n{new_body}")


def send_approved_reply(conn, settings: Settings, reply_id: int) -> dict:
    """Called once, after the REPLY approval was consumed. Never retried."""
    from .mailbox import MailError, get_mailbox
    r = one(conn, "SELECT * FROM reply_draft WHERE id=?", (reply_id,))
    if r["status"] != "sending":
        return {"sent": False, "error": f"reply is {r['status']}"}
    try:
        mb = get_mailbox(settings)
        mid = mb.send(r["recipient"], r["subject"], r["body"], r["in_reply_to"], r["thread_id"])
        with tx(conn):
            conn.execute("UPDATE reply_draft SET status='sent', sent_at=? WHERE id=?", (iso(), reply_id))
            audit(conn, "approval:REPLY", "mail.send", "reply_draft", reply_id, "ok", mid)
        return {"sent": True}
    except (MailError, Exception) as e:  # noqa: BLE001 - recorded, not retried
        with tx(conn):
            conn.execute("UPDATE reply_draft SET status='failed', error=? WHERE id=?", (str(e)[:500], reply_id))
            audit(conn, "approval:REPLY", "mail.send", "reply_draft", reply_id, "failed", str(e))
        return {"sent": False, "error": str(e)}
