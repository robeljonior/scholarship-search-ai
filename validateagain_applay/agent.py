"""AGENT 2 - APPLICANT. Steps 2-7.

run_applicant():
  DISCOVERED/BELOW_THRESHOLD -> validate again (fresh fetch of the official page) -> eligibility -> score
     -> SHORTLISTED | BELOW_THRESHOLD | INELIGIBLE | EXPIRED
  SHORTLISTED -> APPLY approval (#1) -> AWAITING_APPLY
  PREPARING (approval #1 consumed) -> packet -> READY_TO_FILL
Filling and submitting run in the browser worker (worker.py), started by the user or the app,
because the user must be present to sign in, tick declarations and pay.
"""
from __future__ import annotations

import json
from datetime import datetime

from shared import approvals, vault
from shared.audit import audit, emit
from shared.config import Settings, iso, now
from shared.db import one, rows, tx
from shared.fetcher import FetchError, get_fetcher
from shared.jobs import job
from shared.ollama_client import get_llm
from shared.state_machine import transition

from search_find_validate.extract import extract
from search_find_validate.validate import validate
from shared.extract_text import parse_html

from . import eligibility, prepare, scoring


def revalidate(conn, settings: Settings, app: dict, opp: dict, fetcher, llm) -> list[str]:
    """Second, independent check of the official page. Returns notes; updates changed facts."""
    notes = []
    try:
        page = fetcher(opp["official_url"])
    except FetchError as e:
        return [f"could not re-fetch official page: {e}"]
    title = ""
    if page.html:
        p = parse_html(page.html)
        title = (p.h1 or p.title).strip()
    data, _ = extract(page.text, title, llm)
    clean, errors, _ = validate(data, page.final_url)
    changes = {}
    for k in ("deadline", "funding_type"):
        new = clean.get(k)
        if new and str(new) != str(opp.get(k)) and not (k == "funding_type" and new == "unknown"):
            changes[k] = (opp.get(k), new)
    with tx(conn):
        if changes:
            sets = ", ".join(f"{k}=?" for k in changes)
            conn.execute(f"UPDATE opportunity SET {sets}, updated_at=?, verified_at=NULL WHERE id=?",
                         [v[1] for v in changes.values()] + [iso(), opp["id"]])
            audit(conn, "applicant", "opportunity.changed", "opportunity", opp["id"], "ok",
                  {k: list(v) for k, v in changes.items()})
            emit(conn, "opportunity.changed", {"opportunity_id": opp["id"], "changes": {k: list(v) for k, v in changes.items()}})
            notes += [f"{k} changed on official page: {a} -> {b}" for k, (a, b) in changes.items()]
        else:
            conn.execute("UPDATE opportunity SET rechecked_at=? WHERE id=?", (iso(), opp["id"]))
            notes.append("official page re-checked; key facts unchanged")
        # requirements: fill gaps from the fresh extraction, never silently overwrite existing values
        req = json.loads(opp["requirements_json"] or "{}")
        for k, v in (clean.get("requirements") or {}).items():
            if v not in (None, [], "") and req.get(k) in (None, [], ""):
                req[k] = v
        conn.execute("UPDATE opportunity SET requirements_json=? WHERE id=?", (json.dumps(req), opp["id"]))
    return notes


def rank_one(conn, settings: Settings, app_id: int, fetcher, llm, profile: dict, do_revalidate=True) -> dict:
    app = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    opp = one(conn, "SELECT * FROM opportunity WHERE id=?", (app["opportunity_id"],))
    notes = revalidate(conn, settings, app, opp, fetcher, llm) if do_revalidate else []
    opp = one(conn, "SELECT * FROM opportunity WHERE id=?", (app["opportunity_id"],))
    req = json.loads(opp["requirements_json"] or "{}")
    el = eligibility.check(profile, opp, req)
    sc = scoring.score(profile, opp, el, settings["weights"], settings["region_preference"])
    if el["fails"] == ["deadline passed"]:
        new, next_action = "EXPIRED", "none (deadline passed)"
    elif el["class"] == "INELIGIBLE":
        new, next_action = "INELIGIBLE", "none"
    elif el["fails"]:
        new, next_action = "BELOW_THRESHOLD", ("page suggests you miss a requirement; check it, then mark the "
                                               "opportunity verified or update your profile")
    elif sc["score"] >= settings["shortlist_threshold"] and (sc["days_left"] is None or sc["days_left"] >= settings["min_prep_days"]):
        new, next_action = "SHORTLISTED", "approve #1 to start preparing"
    else:
        new, next_action = "BELOW_THRESHOLD", "kept for reference; below shortlist threshold or too little time"
    if el["unknowns"]:
        next_action += f"; verify: {el['unknowns'][0]}"
    with tx(conn):
        conn.execute("""UPDATE application SET eligibility_class=?, score=?, weights_version=?, breakdown_json=?,
                        reasons_json=?, unknowns_json=?, next_action=?, updated_at=? WHERE id=?""",
                     (el["class"], sc["score"], settings["weights_version"], json.dumps(sc["breakdown"]),
                      json.dumps({"passes": el["passes"], "fails": el["fails"], "score": sc["reasons"], "recheck": notes}),
                      json.dumps(el["unknowns"]), next_action, iso(), app_id))
        for u in el["unknowns"]:
            conn.execute("""INSERT INTO task(application_id, kind, title, created_at) VALUES (?,?,?,?)
                            ON CONFLICT(application_id, title) DO NOTHING""", (app_id, "verify", f"Verify: {u}"[:200], iso()))
        if new != app["status"]:
            transition(conn, app_id, new, "applicant", f"{el['class']}, score {sc['score']}")
    return {"application_id": app_id, "class": el["class"], "score": sc["score"], "status": new}


def apply_summary(conn, app_id: int) -> str:
    a = one(conn, """SELECT a.*, o.name, o.funding_type, o.amount, o.currency, o.deadline, o.deadline_tz, o.official_url,
                     o.rechecked_at, o.verified_at FROM application a JOIN opportunity o ON o.id=a.opportunity_id WHERE a.id=?""",
            (app_id,))
    bd = json.loads(a["breakdown_json"] or "{}")
    unknowns = json.loads(a["unknowns_json"] or "[]")
    funding = {"full": "full funding", "partial": "partial funding"}.get(a["funding_type"], "funding unclear")
    if a["amount"]:
        funding += f" ({a['currency']} {a['amount']:,.0f})"
    checked = "verified by you" if a["verified_at"] else ("re-checked " + a["rechecked_at"][:10] if a["rechecked_at"] else "unverified")
    return (f"#{app_id} {a['name']}\n{funding}, score {a['score']:g} "
            f"(funding {bd.get('funding', 0):g}, fit {bd.get('fit', 0):g}, readiness {bd.get('readiness', 0):g}, "
            f"location {bd.get('location', 0):g})\n{a['eligibility_class'].replace('_', ' ').lower()}"
            f"{'; unknown: ' + '; '.join(unknowns[:3]) if unknowns else ''}\n"
            f"Deadline {a['deadline']}{' ' + a['deadline_tz'] if a['deadline_tz'] else ''} ({checked})\n"
            f"{a['official_url']}\nApply?")


def request_apply(conn, settings: Settings, app_id: int) -> int:
    transition(conn, app_id, "AWAITING_APPLY", "applicant", "asking approval #1")
    app = one(conn, "SELECT o.deadline FROM application a JOIN opportunity o ON o.id=a.opportunity_id WHERE a.id=?", (app_id,))
    hard = None
    if app["deadline"]:
        try:
            hard = datetime.fromisoformat(app["deadline"][:10] + "T23:59:00+00:00")
        except ValueError:
            pass
    return approvals.create(conn, settings, "APPLY", app_id, apply_summary(conn, app_id), hard_deadline=hard)


def prepare_ready(conn, settings: Settings, llm=None) -> list[int]:
    """Step 4: build packets for every application whose approval #1 was granted."""
    llm = llm if llm is not None else get_llm(settings)
    built = []
    for a in rows(conn, "SELECT id FROM application WHERE status='PREPARING' ORDER BY id"):
        prepare.build_packet(conn, settings, a["id"], llm)
        transition(conn, a["id"], "READY_TO_FILL", "applicant", "packet built")
        built.append(a["id"])
    return built


def recheck_all(conn, settings: Settings, profile: dict) -> dict:
    """Check EVERY open application against the current profile (no page fetch).

    If the profile changed since an application was ranked, its stored eligibility is stale:
      SHORTLISTED / AWAITING_APPLY / INELIGIBLE -> pending approval #1 is invalidated and the application goes
        back to DISCOVERED, so it is ranked again in this run;
      PREPARING .. AWAITING_SUBMIT -> status is left alone (work already approved) but a task warns the user.
    """
    out = {"checked": 0, "reset": [], "warned": []}
    late = ("PREPARING", "READY_TO_FILL", "FILLING", "NEEDS_USER", "AWAITING_SUBMIT")
    q = "SELECT * FROM application WHERE status IN ('SHORTLISTED','AWAITING_APPLY','INELIGIBLE'," + ",".join("?" * len(late)) + ") ORDER BY id"
    for app in rows(conn, q, late):
        opp = one(conn, "SELECT * FROM opportunity WHERE id=?", (app["opportunity_id"],))
        el = eligibility.check(profile, opp, json.loads(opp["requirements_json"] or "{}"))
        sc = scoring.score(profile, opp, el, settings["weights"], settings["region_preference"])
        old = json.loads(app["reasons_json"] or "{}")
        out["checked"] += 1
        same = (el["class"] == app["eligibility_class"] and sorted(el["fails"]) == sorted(old.get("fails", []))
                and abs((sc["score"] or 0) - (app["score"] or 0)) < 0.5)
        if same:
            continue
        with tx(conn):
            if app["status"] in late:
                problems = el["fails"] + el["unknowns"]
                if problems:
                    conn.execute("""INSERT INTO task(application_id, kind, title, created_at) VALUES (?,?,?,?)
                                    ON CONFLICT(application_id, title) DO NOTHING""",
                                 (app["id"], "verify", f"Profile changed: {problems[0]}"[:200], iso()))
                    out["warned"].append(app["id"])
                continue
            if app["status"] == "AWAITING_APPLY":
                for a in conn.execute("""SELECT id FROM approval WHERE action_type='APPLY' AND resource_id=?
                                         AND decision='pending'""", (app["id"],)).fetchall():
                    conn.execute("UPDATE approval SET decision='invalidated', decided_at=? WHERE id=?", (iso(), a["id"]))
                    audit(conn, "applicant", "approval.invalidate", "approval", a["id"], "invalidated", "profile changed")
                    emit(conn, "approval.invalidated", {"approval_id": a["id"], "type": "APPLY"})
                transition(conn, app["id"], "SHORTLISTED", "applicant", "profile changed; approval #1 withdrawn")
            transition(conn, app["id"], "DISCOVERED", "applicant", "profile changed; checking again")
            out["reset"].append(app["id"])
    return out


def run_applicant(conn, settings: Settings, fetcher=None, llm=None, revalidate_pages: bool = True) -> dict:
    with job(conn, "applicant") as summary:
        fetcher = fetcher or get_fetcher(settings)
        llm = llm if llm is not None else get_llm(settings)
        profile = vault.load_profile(settings)
        approvals.expire_due(conn)
        rc = recheck_all(conn, settings, profile)
        summary["rechecked"] = rc["checked"]
        summary["reset_after_profile_change"] = rc["reset"]
        summary["warned_after_profile_change"] = rc["warned"]
        ranked = []
        for a in rows(conn, "SELECT id FROM application WHERE status IN ('DISCOVERED','BELOW_THRESHOLD') ORDER BY id"):
            ranked.append(rank_one(conn, settings, a["id"], fetcher, llm, profile, revalidate_pages))
        summary["ranked"] = len(ranked)
        summary["shortlisted"] = sum(1 for r in ranked if r["status"] == "SHORTLISTED")

        asked = 0
        for a in rows(conn, """SELECT a.id FROM application a WHERE a.status='SHORTLISTED' ORDER BY a.score DESC"""):
            if asked >= settings["max_apply_requests_per_run"]:
                break
            request_apply(conn, settings, a["id"])
            asked += 1
        summary["apply_requests"] = asked

        summary["packets_built"] = len(prepare_ready(conn, settings, llm))
        summary["ready_to_fill"] = [r["id"] for r in rows(conn, "SELECT id FROM application WHERE status='READY_TO_FILL'")]
        summary["awaiting_submit"] = [r["id"] for r in rows(conn, "SELECT id FROM application WHERE status='AWAITING_SUBMIT'")]
    return summary
