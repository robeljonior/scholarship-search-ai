"""Opportunity extraction: local LLM (schema-constrained) with a rule-based fallback.

Both paths return the same dict; validate.py checks it before anything is stored.
"""
from __future__ import annotations

import re
from datetime import date

from shared.config import now
from shared.ollama_client import LLMError

OPPORTUNITY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "provider": {"type": "string"},
        "university": {"type": "string"},
        "country": {"type": "string"},
        "degree_level": {"type": "string", "enum": ["master", "bachelor", "phd", "other", "unknown"]},
        "field": {"type": "string"},
        "funding_type": {"type": "string", "enum": ["full", "partial", "self_funded", "unknown"]},
        "coverage": {"type": "string"},
        "amount": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
        "deadline": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "deadline_tz": {"type": ["string", "null"]},
        "requirements": {
            "type": "object",
            "properties": {
                "min_gpa": {"type": ["number", "null"]},
                "gpa_scale": {"type": ["number", "null"]},
                "ielts": {"type": ["number", "null"]},
                "toefl": {"type": ["number", "null"]},
                "work_years": {"type": ["number", "null"]},
                "eligible_nationalities": {"type": "array", "items": {"type": "string"}},
                "excluded_nationalities": {"type": "array", "items": {"type": "string"}},
                "open_to_all": {"type": ["boolean", "null"]},
                "required_fields": {"type": "array", "items": {"type": "string"}},
                "documents": {"type": "array", "items": {"type": "string"}},
                "references": {"type": ["integer", "null"]},
            },
        },
    },
    "required": ["name", "degree_level", "funding_type", "deadline"],
}

COUNTRY_REGION = {
    "australia": "Australia", "canada": "Canada", "united states": "USA", "usa": "USA", "u.s.": "USA",
    "germany": "Europe", "france": "Europe", "netherlands": "Europe", "sweden": "Europe", "hungary": "Europe",
    "belgium": "Europe", "italy": "Europe", "spain": "Europe", "portugal": "Europe", "finland": "Europe",
    "denmark": "Europe", "norway": "Europe", "austria": "Europe", "ireland": "Europe", "switzerland": "Europe",
    "united kingdom": "Europe", "uk": "Europe", "poland": "Europe", "czech republic": "Europe", "greece": "Europe",
    "europe": "Europe", "estonia": "Europe", "slovenia": "Europe", "lithuania": "Europe",
}
TZ_ABBR = {"CET": "Europe/Berlin", "CEST": "Europe/Berlin", "GMT": "Europe/London", "BST": "Europe/London",
           "UTC": "UTC", "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney", "AWST": "Australia/Perth",
           "EST": "America/Toronto", "EDT": "America/Toronto", "ET": "America/Toronto", "PST": "America/Vancouver",
           "PT": "America/Vancouver", "MST": "America/Edmonton", "EET": "Europe/Helsinki"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
CURRENCY = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "usd": "USD", "cad": "CAD",
            "aud": "AUD", "£": "GBP", "gbp": "GBP", "sek": "SEK", "huf": "HUF", "chf": "CHF", "nok": "NOK",
            "dkk": "DKK"}

_M = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _M + r",?\s+(\d{4})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[2].lower()[:3]], int(m[1]))),
    (re.compile(_M + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I),
     lambda m: (int(m[3]), MONTHS[m[1].lower()[:3]], int(m[2]))),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: (int(m[3]), int(m[2]), int(m[1]))),
]


def find_dates(text: str) -> list[tuple[int, date]]:
    out = []
    for pat, conv in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                y, mo, d = conv(m)
                out.append((m.start(), date(y, mo, d)))
            except (ValueError, KeyError):
                continue
    return sorted(out)


def find_deadline(text: str) -> tuple[str | None, str | None]:
    dates = find_dates(text)
    if not dates:
        return None, None
    lower = text.lower()
    anchors = [m.start() for m in re.finditer(r"deadline|apply by|applications? (close|due)|closing date|submit .* by", lower)]
    best = None
    if anchors:
        best = min(dates, key=lambda d: min(abs(d[0] - a) for a in anchors))
        if min(abs(best[0] - a) for a in anchors) > 300:
            best = None
    if best is None:
        future = [d for d in dates if d[1] >= now().date()]
        best = future[0] if future else dates[0]
    tz = None
    window = text[best[0]: best[0] + 80]
    m = re.search(r"\b(" + "|".join(TZ_ABBR) + r")\b", window)
    if m:
        tz = TZ_ABBR[m[1]]
    return best[1].isoformat(), tz


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


def rule_extract(text: str, title: str = "") -> dict:
    lower = text.lower()
    name = (title or text.split("\n", 1)[0]).strip()[:200]
    if "master" in lower or "msc" in lower or "m.sc" in lower or "postgraduate" in lower:
        level = "master"
    elif "phd" in lower or "doctoral" in lower:
        level = "phd"
    elif "bachelor" in lower or "undergraduate" in lower:
        level = "bachelor"
    else:
        level = "unknown"
    if re.search(r"self[- ]funded|no (scholarship|funding) (is )?available|tuition fees? (are|is) payable by", lower):
        funding = "self_funded"
    elif re.search(r"fully[- ]funded|full scholarship|full funding|covers? (full )?tuition(,| and) (living|monthly|stipend)|"
                   r"tuition (fee )?waiver and (a )?(monthly )?(stipend|allowance)|full tuition", lower):
        funding = "full"
    elif re.search(r"partial(ly)? (funded|scholarship|tuition)|tuition (fee )?(reduction|discount)|"
                   r"(25|30|40|50|75)\s?% (tuition|of tuition)", lower):
        funding = "partial"
    else:
        funding = "unknown"
    country = None
    for c in sorted(COUNTRY_REGION, key=len, reverse=True):
        if re.search(r"\b" + re.escape(c) + r"\b", lower):
            country = c.title() if len(c) > 3 else c.upper()
            break
    deadline, tz = find_deadline(text)
    amount = currency = None
    m = re.search(r"(€|\$|£|eur|usd|cad|aud|gbp|sek|chf)\s?([\d,.]{3,})|([\d,.]{3,})\s?(eur|euros?|usd|cad|aud|gbp|sek|chf)",
                  text, re.I)
    if m:
        cur = (m.group(1) or m.group(4) or "").lower()
        raw = (m.group(2) or m.group(3) or "").replace(",", "")
        try:
            amount = float(raw.rstrip("."))
            currency = CURRENCY.get(cur)
        except ValueError:
            pass
    req: dict = {
        "min_gpa": _num(r"(?:minimum|min\.?|at least)\s+(?:c?gpa|grade point average)\s+(?:of\s+)?(\d(?:\.\d+)?)", text)
        or _num(r"c?gpa\s+(?:of\s+)?(?:at least\s+)?(\d\.\d+)", text),
        "gpa_scale": _num(r"(?:c?gpa).{0,40}?(?:out of|/)\s*(\d(?:\.\d+)?)", text),
        "ielts": _num(r"ielts[^0-9]{0,40}(\d(?:\.\d)?)", text),
        "toefl": _num(r"toefl(?: ibt)?[^0-9]{0,40}(\d{2,3})", text),
        "work_years": _num(r"(?:at least|minimum(?: of)?)\s+(\d+)\s+years?(?:'|’)?\s+(?:of\s+)?(?:relevant\s+)?(?:work|professional)",
                           text),
        "open_to_all": True if re.search(r"open to (applicants|students|candidates)? ?(of|from)? all (nationalities|countries)|"
                                         r"all nationalities", lower) else None,
        "eligible_nationalities": [],
        "excluded_nationalities": [],
        "required_fields": [],
        "documents": [d for d in ["transcript", "cv", "motivation letter", "statement of purpose", "recommendation",
                                  "passport", "diploma", "language certificate", "research proposal"] if d in lower],
        "references": int(_num(r"(\w+|\d)\s+(?:letters? of recommendation|reference letters?|referees?)", text) or 0) or None,
    }
    m = re.search(r"(?:citizens|nationals) of ([A-Z][a-zA-Z ,]+?)(?: only| are eligible|\.)", text)
    if m and not req["open_to_all"]:
        req["eligible_nationalities"] = [x.strip() for x in re.split(r",| and ", m.group(1)) if x.strip()]
    m = re.search(r"(?:not open to|excluding|except) (?:citizens|nationals|applicants) (?:of|from) ([A-Z][a-zA-Z ,]+?)\.", text)
    if m:
        req["excluded_nationalities"] = [x.strip() for x in re.split(r",| and ", m.group(1)) if x.strip()]
    field = None
    m = re.search(r"(?:master'?s?|msc|m\.sc\.?|ma|meng)\s+(?:of\s+science\s+)?(?:degree\s+)?(?:in|of)\s+([A-Z][A-Za-z &,-]{3,60})", text)
    if m:
        field = m.group(1).strip(" ,-")
        req["required_fields"] = []
    coverage = None
    m = re.search(r"(?:covers?|includes?|scholarship provides?)\s+([^.\n]{10,200})", text, re.I)
    if m:
        coverage = m.group(1).strip()
    return {"name": name, "provider": None, "university": None, "country": country, "degree_level": level,
            "field": field, "funding_type": funding, "coverage": coverage, "amount": amount, "currency": currency,
            "deadline": deadline, "deadline_tz": tz, "requirements": req}


def extract(page_text: str, title: str, llm=None) -> tuple[dict, str]:
    """Returns (opportunity dict, model name). Falls back to rules if the LLM is unavailable or fails."""
    rules = rule_extract(page_text, title)
    if llm is not None and llm.available():
        try:
            data = llm.generate_json(
                "extract",
                "Extract the scholarship or master's programme described on this page. Use YYYY-MM-DD dates. "
                "Use null for anything the page does not state. Do not guess.",
                f"TITLE: {title}\n{page_text}", OPPORTUNITY_SCHEMA)
            # rules fill gaps the model left empty; the model wins where both have a value
            merged = {**rules, **{k: v for k, v in data.items() if v not in (None, "", [], {})}}
            merged["requirements"] = {**rules.get("requirements", {}),
                                      **{k: v for k, v in (data.get("requirements") or {}).items()
                                         if v not in (None, "", [])}}
            return merged, llm.model_for("extract")
        except (LLMError, KeyError, TypeError, ValueError):
            pass
    return rules, "rules"


def region_for(country: str | None) -> str | None:
    return COUNTRY_REGION.get((country or "").lower())
