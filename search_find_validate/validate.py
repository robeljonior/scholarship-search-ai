"""Deterministic validation of extracted opportunities. LLM output is never trusted as-is."""
from __future__ import annotations

import re
from datetime import date

from .extract import region_for


def validate(op: dict, page_url: str) -> tuple[dict, list[str], list[str]]:
    """Returns (cleaned, errors, warnings). Errors make the candidate invalid."""
    errors, warnings = [], []
    clean = dict(op)
    name = (op.get("name") or "").strip()
    if len(name) < 5:
        errors.append("missing name")
    clean["name"] = re.sub(r"\s+", " ", name)[:200]

    level = (op.get("degree_level") or "unknown").lower()
    if level not in ("master", "bachelor", "phd", "other", "unknown"):
        level = "unknown"
    clean["degree_level"] = level
    if level in ("bachelor", "phd"):
        errors.append(f"not a master's opportunity ({level})")
    elif level == "unknown":
        warnings.append("degree level unknown")

    funding = (op.get("funding_type") or "unknown").lower()
    if funding not in ("full", "partial", "self_funded", "unknown"):
        funding = "unknown"
    clean["funding_type"] = funding
    if funding == "self_funded":
        errors.append("self-funded (no scholarship)")

    dl = op.get("deadline")
    if dl:
        try:
            clean["deadline"] = date.fromisoformat(str(dl)[:10]).isoformat()
        except ValueError:
            errors.append(f"deadline not a date: {dl!r}")
            clean["deadline"] = None
    else:
        warnings.append("no deadline")

    amount = op.get("amount")
    if amount is not None:
        try:
            clean["amount"] = float(amount)
            if clean["amount"] <= 0:
                clean["amount"] = None
        except (TypeError, ValueError):
            clean["amount"] = None
    cur = op.get("currency")
    clean["currency"] = cur.upper() if isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur) else None
    if clean.get("amount") and not clean["currency"]:
        warnings.append("amount without currency; amount dropped")
        clean["amount"] = None

    if not re.match(r"^https?://", page_url):
        errors.append("page url is not http(s)")
    clean["region"] = region_for(op.get("country"))
    req = op.get("requirements") or {}
    if not isinstance(req, dict):
        req = {}
    for k in ("min_gpa", "gpa_scale", "ielts", "toefl", "work_years"):
        v = req.get(k)
        try:
            req[k] = float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            req[k] = None
    if req.get("ielts") is not None and not (3 <= req["ielts"] <= 9):
        req["ielts"] = None
    if req.get("min_gpa") is not None and not (0 < req["min_gpa"] <= 100):
        req["min_gpa"] = None
    clean["requirements"] = req
    return clean, errors, warnings
