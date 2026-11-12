"""Steps 5-7: FILL -> pause for APPROVAL #2 -> SUBMIT. The only browser path in the system.

Two portal adapters share one interface:
  SimulatedPortal   no browser; for local testing and the demo UI. Its state lives in the DB,
                    so "the user edits a field in the portal" can be simulated from the UI.
  PlaywrightPortal  real browser with a dedicated persistent profile (data/browser_profile/<domain>).
                    Needs `pip install playwright && playwright install chromium`.

Safety rules enforced here:
  - never clicks submit during fill; stops at the review page
  - anything unexpected (no mapping, CAPTCHA, unknown required field) -> NEEDS_USER
  - submit only when the application is SUBMITTING (approval #2 consumed) AND the live form
    hash equals the hash that was approved; otherwise new screenshots + new approval #2
  - saves the confirmation page and reference number as evidence
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path

from shared import approvals
from shared.audit import audit, emit
from shared.config import PACKAGE_ROOT, Settings, iso, now
from shared.db import kv_get, kv_set, one, tx
from shared.fetcher import domain_of
from shared.state_machine import transition

MAPPINGS_DIR = PACKAGE_ROOT / "validateagain_applay" / "field_mappings"


class NeedsUser(Exception):
    pass


def form_hash(values: dict, docs: list[dict], pages: list[str]) -> str:
    blob = json.dumps({"values": values, "docs": sorted((d.get("key"), d.get("sha256")) for d in docs),
                       "pages": pages}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def load_mapping(portal_url: str) -> dict | None:
    d = domain_of(portal_url)
    for p in MAPPINGS_DIR.glob("*.json"):
        m = json.loads(p.read_text())
        if m.get("domain") and (d == m["domain"] or d.endswith("." + m["domain"])):
            return m
        if m.get("match_url") and portal_url.startswith(m["match_url"]):
            return m
    return None


# ---------------------------------------------------------------------------
# Simulated portal
# ---------------------------------------------------------------------------
SIM_PAGES = [
    ("Personal details", ["full_name", "email", "phone", "date_of_birth", "nationality", "country_of_residence"]),
    ("Education", ["degree_level", "degree_field", "university", "graduation_year", "gpa", "gpa_scale"]),
    ("Language and experience", ["english_test", "english_score", "work_experience_years"]),
    ("Statement of purpose", ["sop"]),
]
SIM_DECLARATION = ("I declare that the information I have provided is true and complete, and I accept the "
                   "programme's privacy notice.")


class SimulatedPortal:
    mode = "simulate"

    def __init__(self, conn, app_id: int):
        self.conn, self.app_id = conn, app_id
        self.key = f"sim_portal:{app_id}"

    def state(self) -> dict:
        return kv_get(self.conn, self.key, {"values": {}, "uploads": [], "submitted": False, "fee": "EUR 0.00"})

    def save(self, st: dict) -> None:
        with tx(self.conn):
            kv_set(self.conn, self.key, st)

    def open(self, url: str) -> None:
        pass

    def logged_in(self) -> bool:
        return True

    def fill(self, packet: dict, shots_dir: Path) -> list[str]:
        st = self.state()
        answers = json.loads(packet["answers_json"])
        answers["sop"] = packet["sop_text"]
        pages = []
        for i, (name, keys) in enumerate(SIM_PAGES, 1):
            for k in keys:
                st["values"][k] = answers.get(k)
            pages.append(name)
            self._shot(shots_dir / f"page{i}.html", name, {k: st["values"].get(k) for k in keys})
        st["uploads"] = [d["key"] for d in json.loads(packet["documents_json"]) if d.get("exists")]
        self.save(st)
        self._shot(shots_dir / "review.html", "Review", st["values"], review=True, uploads=st["uploads"], fee=st["fee"])
        return pages + ["Review"]

    def read_values(self) -> dict:
        return dict(self.state()["values"])

    def review_info(self) -> dict:
        st = self.state()
        return {"fee": st["fee"], "declarations": [SIM_DECLARATION], "uploads": st["uploads"]}

    def user_edit(self, field: str, value: str) -> None:
        """Simulates the user changing a field in the portal after the screenshots."""
        st = self.state()
        st["values"][field] = value
        self.save(st)

    def submit(self) -> dict:
        st = self.state()
        st["submitted"] = True
        ref = "SIM-" + hashlib.sha256(json.dumps(st["values"], sort_keys=True).encode()).hexdigest()[:8].upper()
        self.save(st)
        page = (f"<html><body><h1>Application submitted</h1><p>Reference number: <b id='ref'>{ref}</b></p>"
                f"<p>Submitted at {iso()}</p></body></html>")
        return {"reference": ref, "html": page}

    def screenshot(self, path: Path, label: str) -> None:
        self._shot(path, label, self.read_values())

    def close(self) -> None:
        pass

    @staticmethod
    def _shot(path: Path, title: str, values: dict, review=False, uploads=None, fee=None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows_ = "".join(f"<tr><th>{html.escape(str(k))}</th><td><pre>{html.escape(str(v))[:3000]}</pre></td></tr>"
                        for k, v in values.items())
        extra = ""
        if review:
            extra = (f"<p><b>Uploaded:</b> {html.escape(', '.join(uploads or []))}</p><p><b>Fee:</b> {fee}</p>"
                     f"<p><b>Declaration:</b> {html.escape(SIM_DECLARATION)}</p>")
        path.write_text(f"<html><body style='font-family:sans-serif'><h2>Simulated portal: {html.escape(title)}</h2>"
                        f"<table border=1 cellpadding=4>{rows_}</table>{extra}</body></html>")


# ---------------------------------------------------------------------------
# Playwright portal
# ---------------------------------------------------------------------------
class PlaywrightPortal:
    mode = "playwright"

    def __init__(self, settings: Settings, portal_url: str, mapping: dict | None, headless: bool = False):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise NeedsUser("Playwright is not installed: pip install playwright && playwright install chromium") from e
        self.mapping = mapping
        self.url = portal_url
        profile_dir = settings.browser_profile_dir / domain_of(portal_url)
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(str(profile_dir), headless=headless,
                                                                accept_downloads=False)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.uploads: list[str] = []

    def open(self, url: str) -> None:
        self.page.goto(self.mapping.get("start_url", url) if self.mapping else url, wait_until="domcontentloaded")

    def logged_in(self) -> bool:
        sel = (self.mapping or {}).get("logged_in_selector")
        return True if not sel else self.page.locator(sel).count() > 0

    def wait_for_login(self, timeout_s: int = 900) -> None:
        end = time.time() + timeout_s
        while time.time() < end:
            if self.logged_in():
                return
            time.sleep(3)
        raise NeedsUser("login not completed in the worker's browser window")

    def _check_blockers(self) -> None:
        content = self.page.content().lower()
        if re.search(r"captcha|recaptcha|hcaptcha|i'?m not a robot", content):
            raise NeedsUser("CAPTCHA on the page")

    def fill(self, packet: dict, shots_dir: Path) -> list[str]:
        answers = json.loads(packet["answers_json"])
        answers["sop"] = packet["sop_text"]
        docs = {d["key"]: d for d in json.loads(packet["documents_json"])}
        if not self.mapping:
            raise NeedsUser("no field mapping for this portal yet; add field_mappings/<portal>.json")
        names = []
        for i, pg in enumerate(self.mapping["pages"], 1):
            self._check_blockers()
            if pg.get("marker") and self.page.locator(pg["marker"]).count() == 0:
                raise NeedsUser(f"unexpected page (expected '{pg['name']}')")
            for f in pg["fields"]:
                loc = self.page.locator(f["selector"])
                if loc.count() == 0:
                    if f.get("required", True):
                        raise NeedsUser(f"field not found: {f['selector']} on '{pg['name']}'")
                    continue
                t = f.get("type", "text")
                if t == "file":
                    d = docs.get(f["key"])
                    if not d or not d.get("exists"):
                        raise NeedsUser(f"document missing for upload: {f['key']}")
                    loc.set_input_files(d["path"])
                    self.uploads.append(f["key"])
                elif t == "select":
                    loc.select_option(label=str(answers.get(f["key"]) or ""))
                elif t == "check":
                    raise NeedsUser("declarations are ticked by the user, not the worker")
                else:
                    loc.fill(str(answers.get(f["key"]) or ""))
            self.page.screenshot(path=str(shots_dir / f"page{i}.png"), full_page=True)
            names.append(pg["name"])
            if pg.get("next"):
                self.page.locator(pg["next"]).click()
                self.page.wait_for_load_state("domcontentloaded")
        rv = self.mapping["review"]
        if self.page.locator(rv["marker"]).count() == 0:
            raise NeedsUser("review page not reached")
        self.page.screenshot(path=str(shots_dir / "review.png"), full_page=True)
        return names + ["Review"]

    def read_values(self) -> dict:
        out = {}
        for f in self.mapping["review"]["readback"]:
            loc = self.page.locator(f["selector"])
            out[f["key"]] = loc.first.inner_text().strip() if loc.count() else None
        return out

    def review_info(self) -> dict:
        rv = self.mapping["review"]
        fee = self.page.locator(rv["fee"]).first.inner_text() if rv.get("fee") and self.page.locator(rv["fee"]).count() else "unknown"
        decl = [self.page.locator(rv["declaration"]).first.inner_text()] if rv.get("declaration") and \
            self.page.locator(rv["declaration"]).count() else []
        return {"fee": fee, "declarations": decl, "uploads": self.uploads}

    def submit(self) -> dict:
        self.page.locator(self.mapping["submit"]).click()
        conf = self.mapping["confirmation"]
        self.page.wait_for_selector(conf["marker"], timeout=60000)
        ref = self.page.locator(conf["reference"]).first.inner_text().strip() if conf.get("reference") else ""
        return {"reference": ref, "html": self.page.content()}

    def screenshot(self, path: Path, label: str) -> None:
        self.page.screenshot(path=str(path), full_page=True)

    def close(self) -> None:
        try:
            self.ctx.close()
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def make_portal(conn, settings: Settings, app_id: int, mode: str, headless: bool = False):
    if mode == "simulate":
        return SimulatedPortal(conn, app_id)
    app = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    return PlaywrightPortal(settings, app["portal_url"], load_mapping(app["portal_url"]), headless)


def _latest_packet(conn, app_id: int) -> dict:
    p = one(conn, "SELECT * FROM packet WHERE application_id=? ORDER BY version DESC LIMIT 1", (app_id,))
    if not p:
        raise NeedsUser("no packet built")
    return p


def _evidence(conn, app_id: int, kind: str, path: Path, note: str = "") -> None:
    sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    conn.execute("INSERT INTO evidence(application_id, kind, path, note, sha256, created_at) VALUES (?,?,?,?,?,?)",
                 (app_id, kind, str(path), note, sha, iso()))


def fill(conn, settings: Settings, app_id: int, portal) -> int:
    """READY_TO_FILL -> FILLING -> AWAITING_SUBMIT (with a SUBMIT approval) or NEEDS_USER."""
    from .prepare import placeholders
    packet = _latest_packet(conn, app_id)
    left = placeholders(packet["sop_text"])
    if left:
        raise NeedsUser(f"the SOP still has {len(left)} unfinished placeholder(s), e.g. {left[0][:60]}. "
                        "Edit the SOP first (app: Edit SOP), then fill.")
    transition(conn, app_id, "FILLING", "worker", f"fill started ({portal.mode})")
    app = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    with tx(conn):
        cur = conn.execute("INSERT INTO fill_run(application_id, packet_id, mode, started_at) VALUES (?,?,?,?)",
                           (app_id, packet["id"], portal.mode, iso()))
    run_id = cur.lastrowid
    shots = settings.evidence_dir / str(app_id) / f"fill{run_id}"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        portal.open(app["portal_url"])
        if not portal.logged_in():
            emit_needs(conn, app_id, "please sign in to the portal in the worker's browser window")
            if hasattr(portal, "wait_for_login"):
                portal.wait_for_login()
        pages = portal.fill(packet, shots)
        return _pause_for_review(conn, settings, app_id, run_id, portal, packet, pages, shots)
    except NeedsUser as e:
        _needs_user(conn, app_id, run_id, str(e), portal, shots)
        return run_id
    except Exception as e:  # noqa: BLE001 - never guess; stop and tell the user
        _needs_user(conn, app_id, run_id, f"unexpected error: {type(e).__name__}: {e}", portal, shots)
        return run_id


def emit_needs(conn, app_id: int, msg: str) -> None:
    with tx(conn):
        emit(conn, "worker.needs_user", {"application_id": app_id, "message": msg})


def _needs_user(conn, app_id, run_id, problem, portal, shots) -> None:
    try:
        portal.screenshot(shots / "problem.png" if portal.mode == "playwright" else shots / "problem.html", "problem")
    except Exception:  # noqa: BLE001
        pass
    with tx(conn):
        conn.execute("UPDATE fill_run SET finished_at=?, outcome='needs_user', problem=? WHERE id=?", (iso(), problem, run_id))
        transition(conn, app_id, "NEEDS_USER", "worker", problem)
        emit(conn, "worker.needs_user", {"application_id": app_id, "message": problem})


def _pause_for_review(conn, settings, app_id, run_id, portal, packet, pages, shots) -> int:
    values = portal.read_values()
    docs = json.loads(packet["documents_json"])
    h = form_hash(values, docs, pages)
    info = portal.review_info()
    with tx(conn):
        conn.execute("""UPDATE fill_run SET finished_at=?, outcome='paused_for_review', pages_json=?, form_values_json=?,
                        form_state_hash=? WHERE id=?""",
                     (iso(), json.dumps(pages), json.dumps(values), h, run_id))
        for f in sorted(shots.iterdir()):
            _evidence(conn, app_id, "screenshot", f, f"fill {run_id}: {f.stem}")
        transition(conn, app_id, "AWAITING_SUBMIT", "worker", "filled; paused at review page")
    app = one(conn, """SELECT a.*, o.name, o.deadline FROM application a JOIN opportunity o ON o.id=a.opportunity_id
                       WHERE a.id=?""", (app_id,))
    sop_words = len((values.get("sop") or packet["sop_text"] or "").split())
    summary = (f"SUBMIT #{app_id} {app['name']}\n{len(pages)} pages filled, screenshots attached in the app.\n"
               f"SOP: {sop_words} words. Uploads: {', '.join(info.get('uploads') or []) or 'none'}.\n"
               f"Fee: {info.get('fee')}. Declarations: {len(info.get('declarations') or [])} (you tick them in the portal).\n"
               f"Deadline: {app['deadline']}. Approve = submit exactly this form.")
    hard = None
    if app["deadline"]:
        try:
            hard = datetime.fromisoformat(app["deadline"][:10] + "T23:59:00+00:00")
        except ValueError:
            pass
    approvals.create(conn, settings, "SUBMIT", run_id, summary, hard_deadline=hard)
    return run_id


def submit(conn, settings: Settings, app_id: int, portal) -> dict:
    """Only after approval #2 was consumed (status SUBMITTING). Re-reads the live form first."""
    app = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    if app["status"] != "SUBMITTING":
        raise NeedsUser(f"cannot submit: status is {app['status']} (approval #2 not granted)")
    appr = one(conn, """SELECT * FROM approval WHERE action_type='SUBMIT' AND decision='approved' AND consumed_at IS NOT NULL
                        AND json_extract(payload_json,'$.application_id')=? ORDER BY id DESC LIMIT 1""", (app_id,))
    if not appr:
        raise NeedsUser("no consumed SUBMIT approval found")
    approved = json.loads(appr["payload_json"])
    run = one(conn, "SELECT * FROM fill_run WHERE id=?", (approved["fill_run_id"],))
    packet = one(conn, "SELECT * FROM packet WHERE id=?", (run["packet_id"],))
    live = portal.read_values()
    h = form_hash(live, json.loads(packet["documents_json"]), json.loads(run["pages_json"]))
    if h != approved["form_state_hash"]:
        with tx(conn):
            audit(conn, "worker", "submit.blocked", "application", app_id, "blocked", "form changed after approval #2")
            conn.execute("UPDATE fill_run SET outcome='blocked', problem='form changed after approval' WHERE id=?", (run["id"],))
            transition(conn, app_id, "READY_TO_FILL", "worker", "form changed after approval #2; new review needed")
            transition(conn, app_id, "FILLING", "worker", "re-capturing the changed form")
            cur = conn.execute("INSERT INTO fill_run(application_id, packet_id, mode, started_at) VALUES (?,?,?,?)",
                               (app_id, packet["id"], portal.mode, iso()))
        new_run = cur.lastrowid
        shots = settings.evidence_dir / str(app_id) / f"fill{new_run}"
        shots.mkdir(parents=True, exist_ok=True)
        portal.screenshot(shots / ("review.png" if portal.mode == "playwright" else "review.html"), "Review (changed)")
        _pause_for_review(conn, settings, app_id, new_run, portal, packet, json.loads(run["pages_json"]), shots)
        return {"submitted": False, "reason": "form changed after approval #2; a new SUBMIT approval was requested"}

    result = portal.submit()
    d = settings.evidence_dir / str(app_id)
    d.mkdir(parents=True, exist_ok=True)
    conf = d / "confirmation.html"
    conf.write_text(result["html"])
    with tx(conn):
        _evidence(conn, app_id, "confirmation", conf, f"reference {result.get('reference')}")
        conn.execute("UPDATE application SET reference_number=? WHERE id=?", (result.get("reference"), app_id))
        conn.execute("UPDATE fill_run SET outcome='submitted' WHERE id=?", (run["id"],))
        transition(conn, app_id, "SUBMITTED", "worker", f"submitted; reference {result.get('reference')}")
        audit(conn, "worker", "submit", "application", app_id, "ok", result.get("reference") or "")
    return {"submitted": True, "reference": result.get("reference"), "confirmation": str(conf)}


def run_worker(conn, settings: Settings, app_id: int, mode: str = "playwright", headless: bool = False,
               poll_s: int = 3) -> dict:
    """Long-running worker for real portals: fill, keep the browser open, wait for approval #2, submit."""
    portal = make_portal(conn, settings, app_id, mode, headless)
    try:
        fill(conn, settings, app_id, portal)
        while True:
            status = one(conn, "SELECT status FROM application WHERE id=?", (app_id,))["status"]
            if status == "SUBMITTING":
                res = submit(conn, settings, app_id, portal)
                if res.get("submitted"):
                    return res
                continue  # form changed: a new approval was requested; keep waiting
            if status != "AWAITING_SUBMIT":
                return {"submitted": False, "status": status}
            approvals.expire_due(conn)
            time.sleep(poll_s)
    finally:
        portal.close()
