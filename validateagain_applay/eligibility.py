"""Hard-requirement check. Deterministic rules, no LLM.

ELIGIBLE              all known mandatory requirements met
POTENTIALLY_ELIGIBLE  no confirmed failure, but something is unknown or unverified
INELIGIBLE            a *verified* mandatory requirement is not met (or the deadline passed)

Unknown is not failure: it becomes a verification task.
"""
from __future__ import annotations

from datetime import date

from shared.config import now

IELTS_TO_TOEFL = {5.5: 46, 6.0: 60, 6.5: 79, 7.0: 94, 7.5: 102, 8.0: 110, 8.5: 115, 9.0: 118}


def _guess_scale(v: float) -> float:
    if v <= 4.0:
        return 4.0
    if v <= 5.0:
        return 5.0
    if v <= 10.0:
        return 10.0
    if v <= 20.0:
        return 20.0
    return 100.0


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def check(profile: dict, opp: dict, req: dict) -> dict:
    passes, fails, unknowns = [], [], []

    # deadline
    if opp.get("deadline"):
        try:
            if date.fromisoformat(opp["deadline"][:10]) < now().date():
                return {"class": "INELIGIBLE", "passes": [], "fails": ["deadline passed"], "unknowns": [], "verified_fail": True}
        except ValueError:
            unknowns.append("deadline format unclear")
    else:
        unknowns.append("deadline not found on the page")

    # degree prerequisite
    lvl = _norm(profile.get("degree", {}).get("level"))
    if lvl in ("bachelor", "master", "bsc", "ba", "beng"):
        passes.append("holds a bachelor's degree")
    else:
        unknowns.append("your degree level is not set in the profile")

    # nationality
    nat = _norm(profile.get("nationality"))
    eligible = [_norm(x) for x in req.get("eligible_nationalities") or []]
    excluded = [_norm(x) for x in req.get("excluded_nationalities") or []]
    if nat and nat in excluded:
        fails.append(f"{profile.get('nationality')} nationals are excluded")
    elif eligible:
        if nat in eligible or any(nat in e or e in nat for e in eligible if e):
            passes.append("nationality is eligible")
        elif any(e in ("developing countries", "africa", "african countries") for e in eligible):
            unknowns.append(f"nationality rule is a country group ({', '.join(eligible)}); check the list")
        else:
            fails.append(f"only open to: {', '.join(req['eligible_nationalities'])}")
    elif req.get("open_to_all"):
        passes.append("open to all nationalities")
    else:
        unknowns.append("nationality rule not stated")

    # GPA
    deg = profile.get("degree", {})
    if req.get("min_gpa"):
        if deg.get("gpa") and deg.get("gpa_scale"):
            scale = req.get("gpa_scale") or _guess_scale(req["min_gpa"])
            need = req["min_gpa"] / scale
            have = float(deg["gpa"]) / float(deg["gpa_scale"])
            note = "" if req.get("gpa_scale") else " (scale assumed)"
            if have + 1e-9 >= need:
                passes.append(f"GPA {deg['gpa']}/{deg['gpa_scale']} meets {req['min_gpa']}/{scale:g}{note}")
            elif note:
                unknowns.append(f"GPA requirement {req['min_gpa']} on an unknown scale; check conversion")
            else:
                fails.append(f"GPA {deg['gpa']}/{deg['gpa_scale']} below {req['min_gpa']}/{scale:g}")
        else:
            unknowns.append("your GPA is not in the profile")

    # English
    eng = profile.get("english", {})
    test, score = _norm(eng.get("test")), eng.get("score")
    if req.get("ielts") or req.get("toefl"):
        if test == "ielts" and req.get("ielts") and score:
            (passes if float(score) >= req["ielts"] else fails).append(f"IELTS {score} vs required {req['ielts']}")
        elif test == "toefl" and req.get("toefl") and score:
            (passes if float(score) >= req["toefl"] else fails).append(f"TOEFL {score} vs required {req['toefl']}")
        elif test == "ielts" and req.get("toefl") and score:
            approx = max((v for k, v in IELTS_TO_TOEFL.items() if k <= float(score)), default=0)
            unknowns.append(f"only TOEFL {req['toefl']} stated; your IELTS {score} ~ TOEFL {approx}; check if IELTS is accepted")
        elif not test:
            unknowns.append("no English test in profile; check for a waiver (e.g. English-medium degree)")
        else:
            unknowns.append(f"English requirement uses a different test than your {eng.get('test')}")

    # work experience
    if req.get("work_years"):
        have = profile.get("work_experience_years")
        if have is None:
            unknowns.append("work experience not in profile")
        elif float(have) >= req["work_years"]:
            passes.append(f"{have} years work experience (needs {req['work_years']:g})")
        else:
            fails.append(f"{have} years work experience, needs {req['work_years']:g}")

    # field
    fields = [_norm(f) for f in req.get("required_fields") or []]
    if fields:
        mine = _norm(deg.get("field"))
        if any(f in mine or mine in f for f in fields):
            passes.append("degree field matches")
        else:
            unknowns.append(f"required background: {', '.join(req['required_fields'])}; yours: {deg.get('field')}")

    verified = bool(opp.get("verified_at"))
    if fails and verified:
        cls = "INELIGIBLE"
    elif fails:
        cls = "POTENTIALLY_ELIGIBLE"
        unknowns = [f"page says you do not meet: {f} (unverified; check)" for f in fails] + unknowns
    elif unknowns:
        cls = "POTENTIALLY_ELIGIBLE"
    else:
        cls = "ELIGIBLE"
    return {"class": cls, "passes": passes, "fails": fails, "unknowns": unknowns, "verified_fail": bool(fails and verified)}
