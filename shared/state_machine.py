"""Application lifecycle. Every status change goes through `transition`.

Ownership rule: Scout creates applications in DISCOVERED. Applicant (and its browser
worker) moves them up to SUBMITTED. After SUBMITTED, only a consumed STATUS approval or
the user may change the status. Email classifiers can only *propose*.
"""
from __future__ import annotations

from .audit import audit, emit
from .config import iso
from .db import tx

TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"SHORTLISTED", "INELIGIBLE", "BELOW_THRESHOLD"},
    "BELOW_THRESHOLD": {"SHORTLISTED", "INELIGIBLE", "DISCOVERED"},
    "INELIGIBLE": {"DISCOVERED"},
    "SHORTLISTED": {"AWAITING_APPLY", "DISCOVERED"},
    "AWAITING_APPLY": {"PREPARING", "SKIPPED", "SHORTLISTED"},
    "PREPARING": {"READY_TO_FILL", "NEEDS_USER"},
    "READY_TO_FILL": {"FILLING", "PREPARING"},
    "FILLING": {"AWAITING_SUBMIT", "NEEDS_USER", "READY_TO_FILL"},
    "NEEDS_USER": {"READY_TO_FILL", "PREPARING"},
    "AWAITING_SUBMIT": {"SUBMITTING", "READY_TO_FILL"},
    "SUBMITTING": {"SUBMITTED", "NEEDS_USER", "READY_TO_FILL"},
    "SUBMITTED": {"UNDER_REVIEW", "INTERVIEW", "WAITING_FOR_DOCUMENTS", "ADMITTED", "REJECTED"},
    "UNDER_REVIEW": {"INTERVIEW", "WAITING_FOR_DOCUMENTS", "ADMITTED", "REJECTED"},
    "INTERVIEW": {"ADMITTED", "REJECTED", "WAITING_FOR_DOCUMENTS", "UNDER_REVIEW"},
    "WAITING_FOR_DOCUMENTS": {"UNDER_REVIEW", "INTERVIEW", "ADMITTED", "REJECTED"},
    "ADMITTED": {"ACCEPTED_BY_USER", "DECLINED_BY_USER"},
    "SKIPPED": {"SHORTLISTED"},
}
TERMINAL = {"REJECTED", "ACCEPTED_BY_USER", "DECLINED_BY_USER", "WITHDRAWN", "EXPIRED"}
PRE_SUBMIT = {"DISCOVERED", "BELOW_THRESHOLD", "INELIGIBLE", "SHORTLISTED", "AWAITING_APPLY", "PREPARING",
              "READY_TO_FILL", "FILLING", "NEEDS_USER", "AWAITING_SUBMIT", "SUBMITTING", "SKIPPED"}
POST_SUBMIT = {"SUBMITTED", "UNDER_REVIEW", "INTERVIEW", "WAITING_FOR_DOCUMENTS", "ADMITTED"}

# Which actors may move an application *out of* a state.
PRE_SUBMIT_ACTORS = {"scout", "applicant", "worker", "user", "approval:APPLY", "approval:SUBMIT", "system"}
POST_SUBMIT_ACTORS = {"user", "approval:STATUS"}

# States only reachable through a specific approval (or the user directly).
APPROVAL_GATED = {
    "PREPARING": {"approval:APPLY", "user"},
    "SUBMITTING": {"approval:SUBMIT"},
}


class TransitionError(Exception):
    pass


def allowed(old: str, new: str, actor: str) -> tuple[bool, str]:
    if old in TERMINAL:
        return False, f"{old} is terminal"
    if new == "WITHDRAWN":
        return (actor == "user", "only the user can withdraw")
    if new == "EXPIRED":
        return (old in PRE_SUBMIT and actor in {"system", "applicant", "user"}, "only unsubmitted applications expire")
    if new not in TRANSITIONS.get(old, set()):
        return False, f"{old} -> {new} is not a permitted transition"
    if old in POST_SUBMIT or old == "SUBMITTED":
        if actor not in POST_SUBMIT_ACTORS:
            return False, f"after submission only the user or a STATUS approval may change status (actor={actor})"
    elif actor not in PRE_SUBMIT_ACTORS:
        return False, f"actor {actor} may not move a pre-submission application"
    gate = APPROVAL_GATED.get(new)
    if gate and actor not in gate:
        # PREPARING can also be re-entered from READY_TO_FILL/NEEDS_USER when regenerating a packet.
        if not (new == "PREPARING" and old in {"READY_TO_FILL", "NEEDS_USER"} and actor in {"applicant", "user"}):
            return False, f"{new} requires {sorted(gate)}"
    return True, ""


def transition(conn, application_id: int, new: str, actor: str, reason: str = "", source: str = "") -> str:
    with tx(conn):
        row = conn.execute("SELECT status FROM application WHERE id=?", (application_id,)).fetchone()
        if row is None:
            raise TransitionError(f"application {application_id} not found")
        old = row["status"]
        ok, why = allowed(old, new, actor)
        if not ok:
            audit(conn, actor, "transition", "application", application_id, "denied", f"{old}->{new}: {why}")
            raise TransitionError(why)
        conn.execute("UPDATE application SET status=?, updated_at=? WHERE id=?", (new, iso(), application_id))
        conn.execute(
            "INSERT INTO status_history(application_id, old_status, new_status, actor, source, reason, changed_at)"
            " VALUES (?,?,?,?,?,?,?)", (application_id, old, new, actor, source, reason, iso()))
        audit(conn, actor, "transition", "application", application_id, "ok", f"{old}->{new}")
        emit(conn, "application.status", {"application_id": application_id, "old": old, "new": new, "reason": reason})
    return old


def owner_of(status: str) -> str:
    if status in {"DISCOVERED"}:
        return "applicant"  # scout hands off by creating it here
    if status in PRE_SUBMIT:
        return "applicant"
    if status in POST_SUBMIT:
        return "monitor"
    return "none"
