"""Fake/scam check. A candidate is auto-accepted only if every check passes;
anything else waits for the user. Rules are code, not prompts."""
from __future__ import annotations

import re
from datetime import datetime

from shared.config import now

KNOWN_PROVIDERS = {
    "europa.eu", "daad.de", "chevening.org", "si.se", "stipendiumhungaricum.hu", "educanada.ca", "canada.ca",
    "studyaustralia.gov.au", "dfat.gov.au", "australiaawards.gov.au", "fulbrightonline.org", "fulbright.org",
    "campusfrance.org", "studyinholland.nl", "nuffic.nl", "mastercardfdn.org", "cscuk.fcdo.gov.uk",
    "commonwealthscholarships.org", "gov.uk", "studyinsweden.se", "study-in-germany.de", "kaad.de", "kas.de",
    "fes.de", "boell.de", "rosalux.de", "studyinnorway.no", "studyindenmark.dk", "studyinfinland.fi",
    "vliruos.be", "ares-ac.be", "eiffel.campusfrance.org", "joint-master.eu", "emjmd.eu",
    "gatescambridge.org", "rhodeshouse.ox.ac.uk", "aauw.org", "worldbank.org", "adb.org", "isdb.org",
    "orangeknowledge.nl", "swissuniversities.ch", "sbfi.admin.ch", "studyinitaly.esteri.it", "esteri.it",
    "mext.go.jp", "studyinjapan.go.jp", "studyinkorea.go.kr", "csc.edu.cn",
}

AGGREGATORS = {
    "scholars4dev.com", "opportunitydesk.org", "afterschoolafrica.com", "scholarshipportal.com",
    "mastersportal.com", "scholarshipdb.net", "opportunitiesforafricans.com", "youthop.com",
    "worldscholarshipforum.com", "scholarshipsads.com", "scholarshipunion.com", "studyabroad.com",
    "t.me", "telegram.me", "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
}

OFFICIAL_PATTERNS = [
    r"\.edu$", r"\.edu\.[a-z]{2}$", r"\.ac\.[a-z]{2}$", r"\.gov$", r"\.gov\.[a-z]{2}$", r"\.gc\.ca$",
    r"\.europa\.eu$", r"(^|\.)uni-[a-z-]+\.de$", r"(^|\.)tu-[a-z-]+\.de$", r"(^|\.)univ-[a-z-]+\.fr$",
    r"(^|\.)universit[a-z]*\.[a-z.]+$", r"(^|\.)utoronto\.ca$", r"(^|\.)ubc\.ca$", r"(^|\.)mcgill\.ca$",
    r"(^|\.)uwaterloo\.ca$", r"(^|\.)ualberta\.ca$", r"(^|\.)uottawa\.ca$", r"(^|\.)ucalgary\.ca$",
    r"(^|\.)kth\.se$", r"(^|\.)lu\.se$", r"(^|\.)uu\.se$", r"(^|\.)chalmers\.se$", r"(^|\.)ethz\.ch$",
    r"(^|\.)epfl\.ch$", r"(^|\.)tum\.de$", r"(^|\.)rwth-aachen\.de$", r"(^|\.)tudelft\.nl$", r"(^|\.)uva\.nl$",
    r"(^|\.)ku\.dk$", r"(^|\.)dtu\.dk$", r"(^|\.)aalto\.fi$", r"(^|\.)helsinki\.fi$", r"(^|\.)kuleuven\.be$",
    r"(^|\.)ugent\.be$", r"(^|\.)polimi\.it$", r"(^|\.)unibo\.it$", r"(^|\.)uni[a-z]+\.it$",
]

SCAM_PATTERNS = [
    (r"(processing|application|registration|handling|visa)\s+fee\s+(of\s+)?(\$|usd|eur|€|£)?\s*\d+.*(scholarship|award)"
     r"|pay\s+(a\s+)?(\$|usd|eur|€|£)\s*\d+\s*(to|for)\s+(receive|secure|claim|process)", "asks for a fee to receive a scholarship"),
    (r"guarantee[ds]?\s+(admission|visa|acceptance|scholarship)|100\s*%\s*(visa|admission)\s+(guarantee|success)",
     "guaranteed admission/visa"),
    (r"\b(only|just)\s+\d+\s+(slots|seats|places)\s+left|apply\s+(today|now)\s+or\s+lose|limited\s+time\s+offer",
     "pressure tactics"),
    (r"(whatsapp|telegram)\s*(us|me|only)|dm\s+(us|me)\s+(now|to\s+apply)", "contact only via chat"),
    (r"send\s+(your\s+)?(passport|bank\s+details|card\s+number|password)", "asks for sensitive details"),
]
FREEMAIL = r"[A-Za-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook|aol|proton|protonmail|ymail|mail)\.(com|me|net)"


def domain_matches(domain: str, base: str) -> bool:
    return domain == base or domain.endswith("." + base)


def is_aggregator(domain: str) -> bool:
    return any(domain_matches(domain, a) for a in AGGREGATORS)


def is_official_domain(domain: str, extra_official: set[str] | None = None) -> bool:
    domain = domain.lower()
    if is_aggregator(domain):
        return False
    if any(domain_matches(domain, p) for p in KNOWN_PROVIDERS | (extra_official or set())):
        return True
    return any(re.search(p, domain) for p in OFFICIAL_PATTERNS)


def scam_signals(text: str) -> list[str]:
    t = text.lower()
    found = [label for pat, label in SCAM_PATTERNS if re.search(pat, t)]
    has_freemail = re.search(FREEMAIL, text, re.I)
    has_official_link = re.search(r"https?://[^\s]*\.(edu|ac\.[a-z]{2}|gov|europa\.eu)", t)
    if has_freemail and not has_official_link:
        found.append("free-mail contact with no official link")
    return found


def deadline_passed(deadline: str | None) -> bool:
    if not deadline:
        return False
    try:
        d = datetime.fromisoformat(deadline[:10])
    except ValueError:
        return False
    return d.date() < now().date()


def check(opportunity: dict, page_text: str, page_domain: str, lead_classification: str | None = None,
          extra_official: set[str] | None = None) -> tuple[bool, list[str]]:
    reasons = []
    if not is_official_domain(page_domain, extra_official):
        reasons.append(f"not an official domain: {page_domain}")
    signals = scam_signals(page_text)
    reasons += [f"scam signal: {s}" for s in signals]
    if deadline_passed(opportunity.get("deadline")):
        reasons.append(f"deadline passed ({opportunity.get('deadline')})")
    if not opportunity.get("deadline"):
        reasons.append("no deadline found")
    if lead_classification and lead_classification != "opportunity":
        reasons.append(f"came from a post classified as {lead_classification}")
    return (not reasons), reasons
