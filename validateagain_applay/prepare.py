"""Step 4 PREPARE: build an immutable, versioned packet after approval #1.

Packet = form answers (from the vault), checklist, document selection with checksums,
tailored CV, SOP draft, reference-request drafts. Written to packets/<app>/v<n>/.
Drafts are marked ai_generated when the local model wrote them. Nothing is sent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, timedelta

from shared import vault
from shared.audit import audit, emit
from shared.config import Settings, iso, now
from shared.db import one, tx
from shared.ollama_client import LLMError

PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9 ,.'#/()&-]{5,}[^\]]*\]")


def placeholders(text: str | None) -> list[str]:
    """Unfinished bits like [DESCRIBE A CONCRETE PROBLEM...] or [DRAFT - EDIT BEFORE APPROVAL #2]."""
    return PLACEHOLDER_RE.findall(text or "")


STANDARD_DOCS = ["transcript", "diploma", "passport", "english_test", "cv", "statement of purpose"]


def answers_from_profile(profile: dict, opp: dict) -> dict:
    deg, eng = profile.get("degree", {}), profile.get("english", {})
    return {
        "full_name": profile.get("full_name"), "email": profile.get("email"), "phone": profile.get("phone"),
        "date_of_birth": profile.get("date_of_birth"), "nationality": profile.get("nationality"),
        "country_of_residence": profile.get("country_of_residence"),
        "degree_level": deg.get("level"), "degree_field": deg.get("field"), "university": deg.get("university"),
        "graduation_year": deg.get("graduation_year"), "gpa": deg.get("gpa"), "gpa_scale": deg.get("gpa_scale"),
        "english_test": eng.get("test"), "english_score": eng.get("score"),
        "work_experience_years": profile.get("work_experience_years"),
        "program_name": opp.get("name"),
    }


def checklist(opp: dict, req: dict, profile: dict) -> list[dict]:
    items = []
    wanted = list(dict.fromkeys(STANDARD_DOCS + [d for d in (req.get("documents") or [])]))
    for d in wanted:
        items.append({"item": d, "type": "document"})
    refs = req.get("references") or 2
    for i in range(int(refs)):
        items.append({"item": f"reference letter {i + 1}", "type": "reference"})
    if req.get("ielts") or req.get("toefl"):
        items.append({"item": f"English test ({'IELTS ' + str(req['ielts']) if req.get('ielts') else 'TOEFL ' + str(req['toefl'])})",
                      "type": "test"})
    return items


def select_documents(settings: Settings, profile: dict) -> list[dict]:
    out = []
    for key, rel in (profile.get("documents") or {}).items():
        try:
            p = vault.document_path(settings, rel)
        except vault.VaultError:
            continue
        exists = p.exists()
        out.append({"key": key, "path": str(p), "exists": exists,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest() if exists else None})
    return out


def template_cv(profile: dict, opp: dict) -> str:
    deg = profile.get("degree", {})
    lines = [f"# {profile.get('full_name')}", f"{profile.get('email')} | {profile.get('phone')} | {profile.get('nationality')}",
             "", "## Education",
             f"- {deg.get('level', '').title()} in {deg.get('field')}, {deg.get('university')} ({deg.get('graduation_year')}),"
             f" GPA {deg.get('gpa')}/{deg.get('gpa_scale')}", "", "## Experience"]
    for e in profile.get("experience", []):
        lines.append(f"- **{e.get('title')}**, {e.get('org')} ({e.get('from')} - {e.get('to')}): {e.get('summary')}")
    lines += ["", "## Skills", ", ".join(profile.get("skills", [])), "",
              f"## English\n{profile.get('english', {}).get('test')} {profile.get('english', {}).get('score')}"]
    return "\n".join(lines)


def template_sop(profile: dict, opp: dict) -> str:
    deg = profile.get("degree", {})
    exp = (profile.get("experience") or [{}])[0]
    return (
        f"Statement of Purpose - {opp.get('name')}\n\n"
        f"I am applying to {opp.get('name')} because it connects directly to my interest in "
        f"{profile.get('interests', 'my field')}. I hold a {deg.get('level')} degree in {deg.get('field')} from "
        f"{deg.get('university')}, where I graduated with a GPA of {deg.get('gpa')}/{deg.get('gpa_scale')}.\n\n"
        f"Since graduating I have worked as {exp.get('title', 'a professional')} at {exp.get('org', 'my organisation')}, "
        f"where I {(exp.get('summary') or '').rstrip('.').lower() or 'built practical experience'}. This work showed me "
        f"[DESCRIBE A CONCRETE PROBLEM YOU SOLVED AND WHAT YOU LEARNED].\n\n"
        f"Why this programme: [NAME 2-3 SPECIFIC COURSES, LABS OR PROFESSORS FROM THE OFFICIAL PAGE AND WHY THEY FIT].\n\n"
        f"After the programme I plan to {profile.get('career_goal', '[YOUR CAREER GOAL]')}.\n\n"
        f"[DRAFT - EDIT BEFORE APPROVAL #2]\n")


def _facts_ok(text: str, profile: dict) -> bool:
    """No invented facts: every 4-digit year and every capitalised org in the draft must exist in the profile."""
    blob = json.dumps(profile)
    for year in set(re.findall(r"\b(?:19|20)\d{2}\b", text)):
        if year not in blob:
            return False
    return True


def draft_cv(llm, profile: dict, opp: dict) -> tuple[str, bool]:
    base = template_cv(profile, opp)
    if llm is None or not llm.available():
        return base, False
    try:
        out = llm.generate_text(
            "draft", "Reorder and rephrase this CV to emphasise what matters for the programme. Do not add any "
            "employer, degree, date, award or skill that is not already in the CV. Output markdown only.",
            f"PROGRAMME: {opp.get('name')} ({opp.get('field')})\nCV:\n{base}")
    except LLMError:
        return base, False
    return (out, True) if out and _facts_ok(out, profile) else (base, False)


def draft_sop(llm, profile: dict, opp: dict) -> tuple[str, bool]:
    base = template_sop(profile, opp)
    if llm is None or not llm.available():
        return base, False
    facts = {k: profile.get(k) for k in ("degree", "experience", "skills", "interests", "career_goal", "english")}
    try:
        out = llm.generate_text(
            "draft", "Write a 700-900 word statement of purpose draft for this master's application using ONLY "
            "these facts. Leave [PLACEHOLDERS] where specific stories or course names are needed. Plain text.",
            f"PROGRAMME: {opp.get('name')}\nCOVERAGE: {opp.get('coverage')}\nFACTS: {json.dumps(facts)}", max_chars=8000)
    except LLMError:
        return base, False
    if not out or not _facts_ok(out, profile):
        return base, False
    return out + "\n\n[AI DRAFT - EDIT BEFORE APPROVAL #2]\n", True


def reference_drafts(profile: dict, opp: dict) -> list[dict]:
    out = []
    for r in profile.get("referees", []):
        body = (f"Dear {r.get('name')},\n\nI am applying to {opp.get('name')} (deadline {opp.get('deadline')}). "
                f"Would you be willing to write a reference letter for me as my {r.get('relation')}? The portal will "
                f"email you a link once I register you as a referee. I can send my CV and statement of purpose to "
                f"help.\n\nThank you,\n{profile.get('full_name')}")
        out.append({"to": r.get("email"), "name": r.get("name"), "subject": f"Reference request: {opp.get('name')}",
                    "body": body})
    return out


def build_packet(conn, settings: Settings, app_id: int, llm=None, sop_override: str | None = None) -> int:
    app = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    opp = one(conn, "SELECT * FROM opportunity WHERE id=?", (app["opportunity_id"],))
    req = json.loads(opp["requirements_json"] or "{}")
    profile = vault.load_profile(settings)

    answers = answers_from_profile(profile, opp)
    items = checklist(opp, req, profile)
    docs = select_documents(settings, profile)
    cv, cv_ai = draft_cv(llm, profile, opp)
    if sop_override is not None:
        sop, sop_ai = sop_override.strip() + "\n", False
    else:
        sop, sop_ai = draft_sop(llm, profile, opp)
    refs = reference_drafts(profile, opp)

    version = (conn.execute("SELECT MAX(version) FROM packet WHERE application_id=?", (app_id,)).fetchone()[0] or 0) + 1
    d = settings.packets_dir / str(app_id) / f"v{version}"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.packets_dir, 0o700)
    (d / "answers.json").write_text(json.dumps(answers, indent=2))
    (d / "cv.md").write_text(cv)
    (d / "sop.md").write_text(sop)
    (d / "references.json").write_text(json.dumps(refs, indent=2))
    (d / "checklist.json").write_text(json.dumps(items, indent=2))
    canonical = json.dumps({"answers": answers, "docs": docs, "cv": cv, "sop": sop, "refs": refs}, sort_keys=True)
    checksum = hashlib.sha256(canonical.encode()).hexdigest()

    with tx(conn):
        cur = conn.execute(
            """INSERT INTO packet(application_id, version, answers_json, checklist_json, documents_json, cv_text, sop_text,
               reference_drafts_json, checksum, dir, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (app_id, version, json.dumps(answers), json.dumps(items), json.dumps(docs), cv, sop, json.dumps(refs),
             checksum, str(d), iso()))
        pid = cur.lastrowid
        _create_tasks(conn, app_id, opp, items, docs)
        audit(conn, "applicant", "packet.build", "application", app_id, "ok",
              {"version": version, "cv_ai": cv_ai, "sop_ai": sop_ai})
        emit(conn, "packet.built", {"application_id": app_id, "version": version, "documents": len(docs),
                                    "missing_documents": [x["key"] for x in docs if not x["exists"]],
                                    "sop_words": len(sop.split())})
    return pid


def _create_tasks(conn, app_id: int, opp: dict, items: list[dict], docs: list[dict]) -> None:
    deadline = None
    if opp.get("deadline"):
        try:
            deadline = date.fromisoformat(opp["deadline"][:10])
        except ValueError:
            pass
    internal = (deadline - timedelta(days=5)).isoformat() if deadline else None
    have = {x["key"] for x in docs if x["exists"]}
    tasks = [("verify", "Verify deadline and eligibility on the official page", internal),
             ("document", "Edit the SOP draft", internal)]
    for it in items:
        if it["type"] == "document" and it["item"].replace(" ", "_") not in have and it["item"] not in have \
                and it["item"] not in ("cv", "statement of purpose"):
            tasks.append(("document", f"Add {it['item']} to vault/documents", internal))
        if it["type"] == "reference":
            tasks.append(("reference", f"Confirm {it['item']}", internal))
    for kind, title, due in tasks:
        conn.execute("""INSERT INTO task(application_id, kind, title, due_at, created_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(application_id, title) DO NOTHING""", (app_id, kind, title, due, iso()))
