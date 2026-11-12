"""AGENT 1 - SCOUT. Step 1 SEARCH + validate + fake check, then hand off.

Hands off by accepting a candidate: an `opportunity` row plus an `application` in
DISCOVERED (accepted_by = scout, still unverified). Anything suspicious waits for the user.
Returns a JSON-safe summary; never raw page text.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections import Counter

from shared.audit import audit, emit
from shared.config import Settings, iso
from shared.db import one, rows, tx
from shared.extract_text import parse_html
from shared.fetcher import FetchError, domain_of, get_fetcher
from shared.jobs import job
from shared.ollama_client import get_llm

from . import fake_check, leads, registry
from .extract import extract
from .validate import validate

LINK_HINT = re.compile(r"master|msc|m\.sc|scholarship|programme|program|fellowship|award|funding|grant|stipend", re.I)


class Scout:
    def __init__(self, conn, settings: Settings, fetcher=None, llm=None):
        self.conn, self.settings = conn, settings
        self.fetch = fetcher or get_fetcher(settings)
        self.llm = llm if llm is not None else get_llm(settings)
        self.stats = Counter()
        self.extra_official = registry.official_domains(conn)

    # ---- sources -----------------------------------------------------------
    def run_source(self, source: dict) -> None:
        try:
            page = self.fetch(source["url"])
        except FetchError as e:
            with tx(self.conn):
                self.conn.execute("""UPDATE source SET failures=failures+1, last_checked_at=?,
                                     status=CASE WHEN failures+1>=3 THEN 'failing' ELSE status END WHERE id=?""",
                                  (iso(), source["id"]))
                audit(self.conn, "scout", "source.fetch", "source", source["id"], "failed", str(e))
            self.stats["fetch_failed"] += 1
            return
        self.stats["sources_checked"] += 1
        before = self.stats["accepted"] + self.stats["needs_review"]
        if source["tier"] == "lead" or source["source_type"] == "lead":
            self.process_lead_page(source, page)
        elif source["tier"] == "catalog":
            self.process_catalog(source, page)
        else:
            self.process_page(source, page)
        found = self.stats["accepted"] + self.stats["needs_review"] - before
        with tx(self.conn):
            self.conn.execute("""UPDATE source SET last_checked_at=?, content_hash=?, failures=0, status='active',
                                 opportunities_found=opportunities_found+? WHERE id=?""",
                              (iso(), page.sha256, found, source["id"]))

    def process_catalog(self, source: dict, page) -> None:
        links = []
        for href, text in parse_html(page.html).links if page.html else []:
            url = urllib.parse.urljoin(page.final_url, href).split("#")[0]
            d = domain_of(url)
            if not url.startswith("http") or url == page.final_url:
                continue
            if (d == page.domain or fake_check.is_official_domain(d, self.extra_official)) and \
                    (LINK_HINT.search(text or "") or LINK_HINT.search(url)):
                links.append(url)
        links = list(dict.fromkeys(links))[: self.settings["max_catalog_links"]]
        self.stats["catalog_links"] += len(links)
        for url in links:
            if one(self.conn, "SELECT id FROM opportunity WHERE official_url=?", (url,)):
                self.stats["already_known"] += 1
                continue
            try:
                sub = self.fetch(url)
            except FetchError:
                self.stats["fetch_failed"] += 1
                continue
            self.process_page(source, sub)

    def process_lead_page(self, source: dict, page) -> None:
        for post in leads.split_posts(page.html or page.text, page.final_url):
            h = f"{source['id']}:" + hashlib.sha256(post["text"].encode()).hexdigest()
            if one(self.conn, "SELECT id FROM lead WHERE content_hash=?", (h,)):
                continue
            cls, funding, reasons = leads.classify(post["text"])
            official = leads.resolve_official(post["links"], self.extra_official) if cls == "opportunity" else None
            status, drop = "new", None
            if cls != "opportunity":
                status, drop = "dropped", "; ".join(reasons) or cls
            elif not official:
                status, drop = "dropped", "no official page linked"
            else:
                status = "resolved"
            with tx(self.conn):
                cur = self.conn.execute(
                    """INSERT INTO lead(source_id, post_url, excerpt, classification, funding_type, official_url, status,
                       drop_reason, content_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (source["id"], page.final_url, post["text"][:500], cls, funding, official, status, drop, h, iso()))
            lead_id = cur.lastrowid
            self.stats[f"lead_{status}"] += 1
            if status == "resolved":
                if one(self.conn, "SELECT id FROM opportunity WHERE official_url=?", (official,)):
                    self.stats["already_known"] += 1
                    continue
                try:
                    sub = self.fetch(official)
                except FetchError:
                    self.stats["fetch_failed"] += 1
                    continue
                self.process_page(source, sub, lead_id=lead_id, lead_class=cls)

    # ---- one page -> candidate ------------------------------------------------
    def process_page(self, source: dict | None, page, lead_id: int | None = None, lead_class: str | None = None) -> int | None:
        title = ""
        if page.html:
            p = parse_html(page.html)
            title = (p.h1 or p.title).strip()
        data, model = extract(page.text, title, self.llm)
        clean, errors, warnings = validate(data, page.final_url)
        ok, fake_reasons = fake_check.check(clean, page.text, page.domain, lead_class, self.extra_official)
        excerpt = _excerpt(page.text, clean.get("deadline"))

        existing = one(self.conn, "SELECT * FROM opportunity WHERE official_url=?", (page.final_url,))
        if errors:
            status, reasons = "invalid", errors
        elif existing:
            changes = [f"{k}: {existing.get(k)} -> {clean.get(k)}" for k in ("deadline", "funding_type")
                       if clean.get(k) and str(existing.get(k)) != str(clean.get(k))]
            status, reasons = "duplicate", (changes or ["already known"])
            if changes:
                with tx(self.conn):
                    emit(self.conn, "opportunity.changed", {"opportunity_id": existing["id"], "changes": changes})
        elif ok:
            status, reasons = "accepted", warnings
        else:
            status, reasons = "needs_user_review", fake_reasons + warnings

        # Re-scans must not pile up rows: an unchanged known page or an unchanged page that is
        # already waiting for (or was decided by) the user creates nothing new.
        prev = one(self.conn, "SELECT * FROM candidate WHERE url=? ORDER BY id DESC LIMIT 1", (page.final_url,))
        if status == "duplicate" and reasons == ["already known"]:
            self.stats["unchanged"] += 1
            return prev["id"] if prev else None
        if prev and status in ("needs_user_review", "invalid") and prev["content_hash"] == page.sha256 \
                and prev["review_status"] in ("needs_user_review", "rejected", "invalid"):
            self.stats["unchanged"] += 1
            return prev["id"]
        if prev and prev["review_status"] == "needs_user_review":
            with tx(self.conn):
                self.conn.execute("UPDATE candidate SET review_status='duplicate', review_reasons=? WHERE id=?",
                                  (json.dumps(["superseded by a newer fetch of the same page"]), prev["id"]))

        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT INTO candidate(source_id, lead_id, url, extracted_json, excerpt, model, content_hash, fetched_at,
                   review_status, review_reasons, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (source["id"] if source else None, lead_id, page.final_url, json.dumps(clean), excerpt, model,
                 page.sha256, iso(), "pending", json.dumps(reasons), iso()))
            cid = cur.lastrowid
            if status == "accepted":
                accept_candidate(self.conn, cid, actor="scout")
            else:
                self.conn.execute("UPDATE candidate SET review_status=? WHERE id=?", (status, cid))
                if status == "needs_user_review":
                    emit(self.conn, "candidate.needs_review", {"candidate_id": cid, "name": clean.get("name"),
                                                               "reasons": reasons})
            audit(self.conn, "scout", "candidate." + status, "candidate", cid, "ok", {"reasons": reasons})
        key = {"accepted": "accepted", "needs_user_review": "needs_review"}.get(status, status)
        self.stats[key] += 1
        return cid


def _excerpt(text: str, deadline: str | None) -> str:
    if deadline:
        i = text.lower().find("deadline")
        if i >= 0:
            return text[max(0, i - 200): i + 300]
    return text[:500]


def accept_candidate(conn, candidate_id: int, actor: str = "user") -> int:
    """Create the opportunity and the application (DISCOVERED). actor=user also marks it verified."""
    with tx(conn):
        c = one(conn, "SELECT * FROM candidate WHERE id=?", (candidate_id,))
        if not c:
            raise ValueError("candidate not found")
        if c["review_status"] == "accepted":
            return c["opportunity_id"]
        if c["review_status"] in ("invalid", "duplicate") and actor != "user":
            raise ValueError(f"candidate is {c['review_status']}")
        d = json.loads(c["extracted_json"])
        if fake_check.deadline_passed(d.get("deadline")):
            raise ValueError(f"deadline already passed ({d.get('deadline')}); there is nothing to apply to. "
                             "If the page shows a newer deadline, propose the URL again so it is re-extracted.")
        existing = one(conn, "SELECT id FROM opportunity WHERE official_url=?", (c["url"],))
        if existing:
            oid = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO opportunity(candidate_id, name, provider, university, country, region, degree_level, field,
                   funding_type, coverage, amount, currency, deadline, deadline_tz, official_url, official_domain,
                   requirements_json, verified_at, verified_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate_id, d.get("name"), d.get("provider"), d.get("university"), d.get("country"), d.get("region"),
                 d.get("degree_level"), d.get("field"), d.get("funding_type"), d.get("coverage"), d.get("amount"),
                 d.get("currency"), d.get("deadline"), d.get("deadline_tz"), c["url"], domain_of(c["url"]),
                 json.dumps(d.get("requirements") or {}), iso() if actor == "user" else None,
                 "user" if actor == "user" else None, iso(), iso()))
            oid = cur.lastrowid
            conn.execute("""INSERT INTO application(opportunity_id, status, portal_url, created_at, updated_at)
                            VALUES (?, 'DISCOVERED', ?, ?, ?)""", (oid, c["url"], iso(), iso()))
            app_id = conn.execute("SELECT id FROM application WHERE opportunity_id=?", (oid,)).fetchone()[0]
            conn.execute("""INSERT INTO status_history(application_id, old_status, new_status, actor, source, reason, changed_at)
                            VALUES (?, NULL, 'DISCOVERED', ?, ?, 'candidate accepted', ?)""",
                         (app_id, actor, f"candidate:{candidate_id}", iso()))
            emit(conn, "application.discovered", {"application_id": app_id, "name": d.get("name")})
        conn.execute("UPDATE candidate SET review_status='accepted', accepted_by=?, opportunity_id=? WHERE id=?",
                     (actor, oid, candidate_id))
        audit(conn, actor, "candidate.accept", "candidate", candidate_id, "ok")
    return oid


def reject_candidate(conn, candidate_id: int, reason: str = "") -> None:
    with tx(conn):
        conn.execute("UPDATE candidate SET review_status='rejected', review_reasons=? WHERE id=?",
                     (json.dumps([reason or "rejected by user"]), candidate_id))
        audit(conn, "user", "candidate.reject", "candidate", candidate_id, "ok", reason)


def propose_url(conn, settings: Settings, url: str, fetcher=None, llm=None) -> dict:
    """Entry point for URLs found by OpenClaw web search. The tracker fetches, not the agent."""
    scout = Scout(conn, settings, fetcher, llm)
    src = one(conn, "SELECT * FROM source WHERE url=?", (url,))
    try:
        page = scout.fetch(url)
    except FetchError as e:
        return {"ok": False, "error": str(e)}
    cid = scout.process_page(src, page)
    c = one(conn, "SELECT id, review_status, review_reasons FROM candidate WHERE id=?", (cid,))
    return {"ok": True, "candidate_id": cid, "status": c["review_status"], "reasons": json.loads(c["review_reasons"] or "[]")}


def run_scout(conn, settings: Settings, only_due: bool = True, fetcher=None, llm=None,
              source_ids: list[int] | None = None) -> dict:
    with job(conn, "scout") as summary:
        scout = Scout(conn, settings, fetcher, llm)
        if source_ids:
            sources = [s for s in rows(conn, "SELECT * FROM source") if s["id"] in source_ids]
        elif only_due:
            sources = registry.due_sources(conn)
        else:
            sources = rows(conn, "SELECT * FROM source WHERE status != 'disabled'")
        for s in sources:
            scout.run_source(s)
        summary.update(dict(scout.stats))
        summary["llm"] = "ollama" if scout.llm.available() else "rules (ollama not reachable)"
        summary["overdue_sources"] = [c["name"] for c in registry.coverage(conn) if c["state"] in ("overdue", "failing")]
    return summary
