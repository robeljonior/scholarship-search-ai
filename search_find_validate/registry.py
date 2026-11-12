"""Source registry: what to check and how often. `coverage()` shows what was missed."""
from __future__ import annotations

from datetime import datetime, timedelta

from shared.audit import audit
from shared.config import iso, now
from shared.db import rows, tx
from shared.fetcher import domain_of

# Starting registry. URLs change; `tracker discover coverage` shows failing ones to fix.
SEED_SOURCES = [
    ("Erasmus Mundus catalogue", "https://www.eacea.ec.europa.eu/scholarships/erasmus-mundus-catalogue_en", "catalog"),
    ("DAAD scholarship database", "https://www2.daad.de/deutschland/stipendium/datenbank/en/21148-scholarship-database/", "catalog"),
    ("Chevening", "https://www.chevening.org/scholarships/", "catalog"),
    ("Swedish Institute SISGP", "https://si.se/en/apply/scholarships/swedish-institute-scholarships-for-global-professionals/", "catalog"),
    ("Stipendium Hungaricum", "https://stipendiumhungaricum.hu/apply/", "catalog"),
    ("EduCanada scholarships", "https://www.educanada.ca/scholarships-bourses/non_can/opportunities-opportunites.aspx?lang=eng", "catalog"),
    ("Study Australia scholarships", "https://www.studyaustralia.gov.au/en/plan-your-studies/scholarships-and-funding", "catalog"),
    ("Australia Awards", "https://www.dfat.gov.au/people-to-people/australia-awards", "catalog"),
    ("Fulbright Foreign Student Program", "https://foreign.fulbrightonline.org/", "catalog"),
    ("Eiffel Excellence", "https://www.campusfrance.org/en/eiffel-scholarship-program-of-excellence", "catalog"),
    ("Study in Holland scholarships", "https://www.studyinholland.nl/finances/scholarships", "catalog"),
    ("Mastercard Foundation Scholars", "https://mastercardfdn.org/en/what-we-do/our-programs/mastercard-foundation-scholars-program/", "catalog"),
    ("Commonwealth Scholarships", "https://cscuk.fcdo.gov.uk/scholarships/", "catalog"),
]
TIER_INTERVAL = {"catalog": 7, "university": 14, "country": 14, "web": 7, "lead": 2}
TIER_TYPE = {"catalog": "official", "university": "official", "country": "official", "web": "official",
             "lead": "lead"}


def add_source(conn, url: str, tier: str = "web", name: str | None = None, interval: int | None = None,
               source_type: str | None = None) -> int:
    with tx(conn):
        conn.execute(
            """INSERT INTO source(url, domain, name, tier, source_type, check_interval_days, created_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING""",
            (url, domain_of(url), name or domain_of(url), tier, source_type or TIER_TYPE.get(tier, "official"),
             interval or TIER_INTERVAL.get(tier, 7), iso()))
        sid = conn.execute("SELECT id FROM source WHERE url=?", (url,)).fetchone()[0]
        audit(conn, "user", "source.add", "source", sid, "ok", url)
    return sid


def seed(conn) -> int:
    for name, url, tier in SEED_SOURCES:
        add_source(conn, url, tier, name)
    return len(SEED_SOURCES)


def due_sources(conn) -> list[dict]:
    out = []
    for s in rows(conn, "SELECT * FROM source WHERE status != 'disabled' ORDER BY id"):
        if not s["last_checked_at"]:
            out.append(s)
            continue
        last = datetime.fromisoformat(s["last_checked_at"])
        if now() - last >= timedelta(days=s["check_interval_days"]):
            out.append(s)
    return out


def coverage(conn) -> list[dict]:
    out = []
    for s in rows(conn, "SELECT * FROM source ORDER BY tier, id"):
        if not s["last_checked_at"]:
            state = "never checked"
        elif now() - datetime.fromisoformat(s["last_checked_at"]) > timedelta(days=s["check_interval_days"] * 1.5):
            state = "overdue"
        else:
            state = "ok"
        if s["status"] == "failing":
            state = "failing"
        out.append({"id": s["id"], "name": s["name"], "url": s["url"], "tier": s["tier"],
                    "last_checked_at": s["last_checked_at"], "found": s["opportunities_found"],
                    "failures": s["failures"], "state": state})
    return out


def official_domains(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT domain FROM source WHERE source_type='official' AND tier IN ('university','catalog','country')")}
