"""Preference score (0-100) with a breakdown. Not an admission probability."""
from __future__ import annotations

from datetime import date

from shared.config import now


def _ratio(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(profile: dict, opp: dict, elig: dict, weights: dict, region_pref: list[str]) -> dict:
    parts: dict[str, float] = {}
    reasons: list[str] = []

    # funding / affordability
    f = {"full": 1.0, "partial": 0.5, "unknown": 0.2}.get(opp.get("funding_type") or "unknown", 0.2)
    parts["funding"] = f * weights["funding"]
    reasons.append(f"funding: {opp.get('funding_type') or 'unknown'}")

    # academic and field fit
    targets = [t.lower() for t in profile.get("target_fields", [])]
    text = " ".join(str(opp.get(k) or "") for k in ("name", "field")).lower()
    if any(t in text for t in targets):
        fit = 1.0
        reasons.append("field matches your targets")
    elif any(w in text for t in targets for w in t.split() if len(w) > 4):
        fit = 0.6
        reasons.append("field partly matches")
    else:
        fit = 0.3
        reasons.append("field match unclear")
    if elig["class"] == "ELIGIBLE":
        fit = min(1.0, fit + 0.1)
    parts["fit"] = fit * weights["fit"]

    # readiness: time left and open questions
    days = None
    if opp.get("deadline"):
        try:
            days = (date.fromisoformat(opp["deadline"][:10]) - now().date()).days
        except ValueError:
            pass
    if days is None:
        r = 0.4
    elif days >= 60:
        r = 1.0
    elif days >= 30:
        r = 0.75
    elif days >= 14:
        r = 0.45
    elif days >= 0:
        r = 0.15
    else:
        r = 0.0
    r -= 0.1 * len(elig["unknowns"])
    parts["readiness"] = _ratio(r) * weights["readiness"]
    reasons.append(f"{days} days to deadline" if days is not None else "deadline unknown")

    # location
    region = opp.get("region")
    if region in region_pref:
        loc = 1.0 - 0.2 * region_pref.index(region)
    else:
        loc = 0.3
    parts["location"] = _ratio(loc) * weights["location"]
    parts["competitiveness"] = 0.0  # off until real data exists

    if elig["fails"] and not elig["verified_fail"]:
        parts["penalty_unverified_fail"] = -25.0
        reasons.append("page suggests a requirement you may not meet")
    total = max(0.0, round(sum(parts.values()), 1))
    return {"score": total, "breakdown": {k: round(v, 1) for k, v in parts.items()}, "reasons": reasons,
            "days_left": days}
