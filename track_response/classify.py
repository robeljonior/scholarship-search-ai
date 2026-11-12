"""Email classification: rules first, local model second. Output is a *proposal* only."""
from __future__ import annotations

import re

from shared.ollama_client import LLMError

CATEGORIES = ["confirmation", "admit", "reject", "interview", "docs_requested", "other"]
RULES = [
    ("reject", r"regret to inform|unable to offer|not been successful|unsuccessful|we cannot offer|not selected"),
    ("admit", r"congratulations|pleased to (inform|offer)|offer of admission|admission offer|you have been (selected|admitted)"),
    ("interview", r"\binterview\b|schedule (a|an) (call|meeting)|video call|selection conversation"),
    ("docs_requested", r"(missing|additional|outstanding) (document|documents|information)|please (upload|submit|provide)"
                       r"|your application is incomplete"),
    ("confirmation", r"(we have )?received your application|application (has been )?(received|submitted)|"
                     r"thank you for (your application|applying)|application confirmation"),
]
PROPOSED_STATUS = {"admit": "ADMITTED", "reject": "REJECTED", "interview": "INTERVIEW",
                   "docs_requested": "WAITING_FOR_DOCUMENTS"}
SCHEMA = {"type": "object", "properties": {"category": {"type": "string", "enum": CATEGORIES},
                                           "confidence": {"type": "number"}}, "required": ["category", "confidence"]}


def classify(subject: str, body: str, llm=None) -> tuple[str, float, str]:
    text = f"{subject}\n{body}".lower()
    for cat, pat in RULES:
        if re.search(pat, text):
            return cat, 0.9, "rules"
    if llm is not None and llm.available():
        try:
            out = llm.generate_json("classify", "Classify this university/scholarship email into one category: "
                                    + ", ".join(CATEGORIES) + ".", f"SUBJECT: {subject}\n{body[:4000]}", SCHEMA)
            if out.get("category") in CATEGORIES:
                return out["category"], float(out.get("confidence") or 0.5), "llm"
        except (LLMError, TypeError, ValueError):
            pass
    return "other", 0.3, "rules"
