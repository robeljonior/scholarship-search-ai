"""Lead sources (Telegram channels, aggregators, agency sites). A post is never evidence:
it becomes a lead, is classified, and is resolved to the official page or dropped."""
from __future__ import annotations

import html as htmlmod
import re
import urllib.parse

from shared.extract_text import normalise, parse_html

from .fake_check import is_aggregator, is_official_domain, scam_signals

ADVERT = [r"our services", r"contact us (today|now)", r"\bdiscount\b", r"consultanc", r"we (will )?help you (get|apply)",
          r"book (a|your) (free )?consultation", r"agency fee", r"service (fee|charge)"]
SELF_FUNDED = [r"self[- ]funded", r"no scholarship", r"tuition fees? (from|starting)"]
BACHELOR_ONLY = [r"\bundergraduate\b(?!.*master)", r"\bbachelor'?s? (only|program|degree)\b(?!.*master)"]


def split_posts(html: str, page_url: str) -> list[dict]:
    """Telegram public preview (t.me/s/<channel>) or any page with post blocks; else one post."""
    blocks = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', html, re.S)
    if not blocks:
        blocks = re.findall(r'<article[^>]*class="[^"]*post[^"]*"[^>]*>(.*?)</article>', html, re.S)
    if not blocks:
        blocks = [html]
    posts = []
    for i, b in enumerate(blocks):
        p = parse_html(b)
        text = normalise(htmlmod.unescape("".join(p.parts)))
        links = [urllib.parse.urljoin(page_url, h) for h, _ in p.links if h and h.startswith(("http", "/"))]
        links += re.findall(r"https?://[^\s<>\"')]+", text)
        posts.append({"index": i, "text": text, "links": list(dict.fromkeys(links))})
    return posts


def classify(text: str) -> tuple[str, str, list[str]]:
    """Returns (classification, funding_type, reasons)."""
    t = text.lower()
    signals = scam_signals(text)
    if signals:
        return "scam_suspect", "unknown", signals
    if any(re.search(p, t) for p in ADVERT):
        return "advert", "unknown", ["agency advert signals"]
    if any(re.search(p, t) for p in SELF_FUNDED):
        return "self_funded", "self_funded", ["self-funded"]
    if any(re.search(p, t) for p in BACHELOR_ONLY) and "master" not in t:
        return "wrong_level", "unknown", ["bachelor's only"]
    if not re.search(r"scholarship|funded|fellowship|stipend|grant|tuition waiver|bursary", t):
        return "irrelevant", "unknown", ["no funding mentioned"]
    if "master" not in t and "msc" not in t and "postgraduate" not in t:
        return "wrong_level", "unknown", ["no master's level mentioned"]
    funding = "full" if re.search(r"fully[- ]funded|full scholarship|full funding", t) else (
        "partial" if "partial" in t else "unknown")
    return "opportunity", funding, []


def resolve_official(links: list[str], extra_official: set[str] | None = None) -> str | None:
    from shared.fetcher import domain_of
    for url in links:
        d = domain_of(url)
        if d and not is_aggregator(d) and is_official_domain(d, extra_official):
            return url
    return None
