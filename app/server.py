"""Local web app: `tracker app`. 127.0.0.1 only, per-start token, same-origin + JSON for changes.

Approvals are NOT possible here unless started with --dev-approvals (local testing); real
approvals go through the Telegram approval bot or `tracker approve` in a terminal.

Also serves a tiny fake application portal at /demo-portal/ so the real Playwright worker
can be tested end to end without touching a real university site.
"""
from __future__ import annotations

import html
import json
import mimetypes
import secrets
import threading
import traceback
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shared import approvals, vault
from shared.config import Settings, load_settings
from shared.db import connect, one, rows, tx

STATIC = Path(__file__).resolve().parent / "static"
LOCK = threading.RLock()
RUNNING: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# read models
# ---------------------------------------------------------------------------
def application_detail(conn, app_id: int) -> dict:
    a = one(conn, "SELECT * FROM application WHERE id=?", (app_id,))
    if not a:
        return {}
    o = one(conn, "SELECT * FROM opportunity WHERE id=?", (a["opportunity_id"],))
    p = one(conn, "SELECT * FROM packet WHERE application_id=? ORDER BY version DESC LIMIT 1", (app_id,))
    runs = rows(conn, "SELECT * FROM fill_run WHERE application_id=? ORDER BY id DESC", (app_id,))
    ev = rows(conn, "SELECT id, kind, note, path, created_at FROM evidence WHERE application_id=? ORDER BY id", (app_id,))
    for e in ev:
        e["url"] = f"/evidence/{e['id']}" if e.get("path") else None
        e.pop("path", None)
    return {
        "application": a, "opportunity": o,
        "history": rows(conn, "SELECT * FROM status_history WHERE application_id=? ORDER BY id", (app_id,)),
        "packet": p, "fill_runs": runs, "evidence": ev,
        "tasks": rows(conn, "SELECT * FROM task WHERE application_id=? ORDER BY status='DONE', due_at", (app_id,)),
        "approvals": rows(conn, """SELECT * FROM approval WHERE (action_type='APPLY' AND resource_id=?)
                                   OR (action_type='SUBMIT' AND json_extract(payload_json,'$.application_id')=?)
                                   OR (action_type='STATUS' AND json_extract(payload_json,'$.application_id')=?)
                                   ORDER BY id DESC""", (app_id, app_id, app_id)),
        "emails": rows(conn, "SELECT * FROM email_record WHERE application_id=? ORDER BY id DESC", (app_id,)),
    }


def overview(conn, settings: Settings, dev: bool) -> dict:
    from shared.cli import agent_status
    from shared.ollama_client import get_llm
    upcoming = rows(conn, """SELECT a.id, a.status, a.score, o.name, o.deadline FROM application a JOIN opportunity o
                             ON o.id=a.opportunity_id WHERE o.deadline IS NOT NULL AND a.status NOT IN
                             ('INELIGIBLE','EXPIRED','WITHDRAWN','REJECTED','SKIPPED','BELOW_THRESHOLD')
                             ORDER BY o.deadline LIMIT 8""")
    return {"status": agent_status(conn), "upcoming": upcoming, "fetch_mode": settings["fetch_mode"],
            "mail_source": settings["mail_source"], "ollama": get_llm(settings).available(),
            "dev_approvals": dev, "pin_set": vault.pin_is_set(settings), "running": RUNNING}


# ---------------------------------------------------------------------------
# background jobs (own DB connection per thread)
# ---------------------------------------------------------------------------
def run_bg(settings: Settings, name: str, fn) -> dict:
    if RUNNING.get(name, {}).get("state") == "running":
        return {"ok": False, "error": f"{name} already running"}
    RUNNING[name] = {"state": "running"}

    def target():
        conn = connect(settings)
        try:
            RUNNING[name] = {"state": "done", "result": fn(conn)}
        except Exception as e:  # noqa: BLE001
            RUNNING[name] = {"state": "failed", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=4)}
        finally:
            conn.close()
    threading.Thread(target=target, daemon=True).start()
    return {"ok": True, "started": name}


def after_decision(conn, settings: Settings, result: dict) -> dict:
    from validateagain_applay.agent import prepare_ready
    if result.get("type") == "APPLY" and result.get("result") == "approved":
        result["prepared"] = prepare_ready(conn, settings)
    return result


# ---------------------------------------------------------------------------
# demo portal (fake external site for Playwright testing)
# ---------------------------------------------------------------------------
PORTAL_STATE: dict = {"values": {}, "submitted": None}
PORTAL_PAGES = [
    ("personal", "Personal details", [("full_name", "Full name"), ("email", "Email"), ("nationality", "Nationality"),
                                      ("date_of_birth", "Date of birth")]),
    ("education", "Education", [("degree_field", "Degree field"), ("university", "University"), ("gpa", "GPA"),
                                ("english_score", "English score")]),
    ("sop", "Statement of purpose", [("sop", "Statement of purpose")]),
]


def portal_page(step: int) -> str:
    head = ("<html><head><title>Demo University Application Portal</title><style>body{font-family:sans-serif;"
            "max-width:720px;margin:2rem auto}label{display:block;margin:.6rem 0}input,textarea{width:100%;padding:.4rem}"
            "</style></head><body><p id='logged-in'>Signed in as demo applicant</p>")
    if step < len(PORTAL_PAGES):
        key, title, fields = PORTAL_PAGES[step]
        inputs = ""
        for f, label in fields:
            val = html.escape(str(PORTAL_STATE["values"].get(f, "")))
            if f == "sop":
                inputs += f"<label>{label}<textarea id='{f}' name='{f}' rows=12>{val}</textarea></label>"
            else:
                inputs += f"<label>{label}<input id='{f}' name='{f}' value='{val}'></label>"
        return (head + f"<h2 id='page-{key}'>{title}</h2><form method='post' action='/demo-portal/step/{step}'>"
                f"{inputs}<button id='next' type='submit'>Save and continue</button></form></body></html>")
    vals = PORTAL_STATE["values"]
    rows_ = "".join(f"<tr><th>{label}</th><td><span id='r-{f}'>{html.escape(str(vals.get(f, '')))}</span></td></tr>"
                    for _, _, fields in PORTAL_PAGES for f, label in fields)
    return (head + "<h2 id='page-review'>Review your application</h2><table border=1 cellpadding=4>" + rows_ +
            "</table><p>Application fee: <b id='fee'>EUR 0.00</b></p>"
            "<form method='post' action='/demo-portal/submit'><label><input type='checkbox' name='declare' required> "
            "<span id='declaration-text'>I declare that all information is true and complete.</span></label>"
            "<a href='/demo-portal/start'>Edit answers</a> <button id='submit' type='submit'>Submit application</button>"
            "</form></body></html>")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "tracker"
    settings: Settings
    token: str
    dev: bool
    conn = None

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, code=200):
        self._send(code, json.dumps(data, default=str).encode(), "application/json")

    def authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "tk" in c and secrets.compare_digest(c["tk"].value, self.token)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw or b"{}")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}

    # -- GET
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path, q = url.path, dict(urllib.parse.parse_qsl(url.query))
        try:
            if path.startswith("/demo-portal"):
                return self.portal_get(path)
            if path == "/":
                if q.get("token") and secrets.compare_digest(q["token"], self.token):
                    return self._send(302, b"", "text/plain", {
                        "Location": "/", "Set-Cookie": f"tk={self.token}; HttpOnly; SameSite=Strict; Path=/"})
                if not self.authed():
                    return self._send(401, b"Open the URL printed by `tracker app` (it contains the access token).",
                                      "text/plain")
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                   "style-src 'self' 'unsafe-inline'; frame-src 'self'; img-src 'self' data:"})
            if not self.authed():
                return self.json({"error": "unauthorised"}, 401)
            if path.startswith("/evidence/"):
                return self.evidence(int(path.rsplit("/", 1)[1]))
            with LOCK:
                return self.api_get(path, q)
        except Exception as e:  # noqa: BLE001
            return self.json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api_get(self, path: str, q: dict):
        c, s = self.conn, self.settings
        if path == "/api/session":
            return self.json({"token": self.token})
        if path == "/api/overview":
            return self.json(overview(c, s, self.dev))
        if path == "/api/sources":
            from search_find_validate.registry import coverage
            return self.json(coverage(c))
        if path == "/api/candidates":
            st = q.get("status")
            sql = ("SELECT c.*, a.status AS application_status FROM candidate c LEFT JOIN application a "
                   "ON a.opportunity_id=c.opportunity_id" + (" WHERE c.review_status=?" if st else "") +
                   " ORDER BY c.id DESC LIMIT 200")
            out = rows(c, sql, (st,) if st else ())
            for r in out:
                r["review_reasons"] = json.loads(r["review_reasons"] or "[]")
            return self.json(out)
        if path == "/api/leads":
            return self.json(rows(c, "SELECT * FROM lead ORDER BY id DESC LIMIT 200"))
        if path == "/api/applications":
            return self.json(rows(c, """SELECT a.id, a.status, a.eligibility_class, a.score, a.next_action, a.unknowns_json,
                                        o.name, o.deadline, o.funding_type, o.country, o.region, o.official_url
                                        FROM application a JOIN opportunity o ON o.id=a.opportunity_id
                                        ORDER BY CASE WHEN a.score IS NULL THEN 1 ELSE 0 END, a.score DESC"""))
        if path.startswith("/api/applications/"):
            return self.json(application_detail(c, int(path.rsplit("/", 1)[1])))
        if path == "/api/approvals":
            sql = "SELECT * FROM approval" + ("" if q.get("all") else " WHERE decision='pending'") + " ORDER BY id DESC LIMIT 100"
            return self.json(rows(c, sql))
        if path == "/api/emails":
            return self.json(rows(c, "SELECT * FROM email_record ORDER BY id DESC LIMIT 100"))
        if path == "/api/replies":
            return self.json(rows(c, "SELECT * FROM reply_draft ORDER BY id DESC LIMIT 100"))
        if path == "/api/events":
            return self.json(rows(c, "SELECT * FROM event_outbox ORDER BY id DESC LIMIT 100"))
        if path == "/api/jobs":
            return self.json({"runs": rows(c, "SELECT * FROM job_run ORDER BY id DESC LIMIT 30"), "running": RUNNING})
        if path == "/api/health":
            from shared.health import health
            return self.json(health(c, s))
        if path == "/api/audit":
            return self.json(rows(c, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200"))
        return self.json({"error": "not found"}, 404)

    def evidence(self, eid: int):
        with LOCK:
            e = one(self.conn, "SELECT * FROM evidence WHERE id=?", (eid,))
        if not e or not e["path"]:
            return self.json({"error": "not found"}, 404)
        p = Path(e["path"]).resolve()
        if self.settings.evidence_dir.resolve() not in p.parents or not p.exists():
            return self.json({"error": "forbidden"}, 403)
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        return self._send(200, p.read_bytes(), ctype, {"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'"})

    # -- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path.startswith("/demo-portal"):
                return self.portal_post(path)
            if not self.authed():
                return self.json({"error": "unauthorised"}, 401)
            if self.headers.get("X-Tracker-Token") != self.token:
                return self.json({"error": "missing token header"}, 403)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.json({"error": "JSON only"}, 415)
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                return self.json({"error": "cross-origin"}, 403)
            data = self.body()
            with LOCK:
                return self.api_post(path, data)
        except Exception as e:  # noqa: BLE001
            return self.json({"error": f"{type(e).__name__}: {e}"}, 400)

    def api_post(self, path: str, d: dict):
        c, s = self.conn, self.settings
        parts = path.strip("/").split("/")
        if parts[:2] == ["api", "run"]:
            return self.json(self.run_agent(parts[2]))
        if path == "/api/sources":
            from search_find_validate.registry import add_source
            return self.json({"source_id": add_source(c, d["url"], d.get("tier", "web"), d.get("name"))})
        if path == "/api/propose-url":
            from search_find_validate.agent import propose_url
            return self.json(propose_url(c, s, d["url"]))
        if parts[:2] == ["api", "candidates"]:
            from search_find_validate.agent import accept_candidate, reject_candidate
            cid, action = int(parts[2]), parts[3]
            if action == "accept":
                try:
                    return self.json({"opportunity_id": accept_candidate(c, cid, "user")})
                except ValueError as e:
                    return self.json({"error": str(e)}, 400)
            reject_candidate(c, cid, d.get("reason", ""))
            return self.json({"ok": True})
        if parts[:2] == ["api", "applications"]:
            return self.json(self.app_action(int(parts[2]), parts[3], d))
        if parts[:2] == ["api", "approvals"]:
            if not self.dev:
                return self.json({"error": "approvals are disabled in the app; use the Telegram bot or "
                                           "`tracker approve <id>` (start the app with --dev-approvals for local testing)"}, 403)
            try:
                res = approvals.decide(c, s, int(parts[2]), bool(d.get("approve")), str(d.get("pin", "")), "app-dev")
            except approvals.ApprovalError as e:
                return self.json({"error": str(e)}, 400)
            return self.json(after_decision(c, s, res))
        if parts[:2] == ["api", "tasks"]:
            with tx(c):
                c.execute("UPDATE task SET status=? WHERE id=?", (d.get("status", "DONE"), int(parts[2])))
            return self.json({"ok": True})
        return self.json({"error": "not found"}, 404)

    def run_agent(self, name: str) -> dict:
        s = self.settings
        if name in ("scout", "scout-all"):
            from search_find_validate.agent import run_scout
            return run_bg(s, "scout", lambda conn: run_scout(conn, s, only_due=(name == "scout")))
        if name == "applicant":
            from validateagain_applay.agent import run_applicant
            return run_bg(s, "applicant", lambda conn: run_applicant(conn, s))
        if name == "monitor":
            from track_response.agent import run_monitor
            return run_bg(s, "monitor", lambda conn: run_monitor(conn, s))
        if name == "run-due":
            from shared.jobs import run_due
            return run_bg(s, "run-due", lambda conn: run_due(conn, s))
        if name == "dispatch":
            from shared.jobs import dispatch
            return dispatch(self.conn, s)
        if name == "backup":
            from shared.health import backup, restore_test
            arch = backup(self.conn, s)
            return {"archive": arch, "restore_test": restore_test(self.conn, s, arch)}
        return {"error": "unknown agent"}

    def app_action(self, app_id: int, action: str, d: dict) -> dict:
        from shared.state_machine import transition
        from validateagain_applay import worker
        c, s = self.conn, self.settings
        if action == "sop":
            from validateagain_applay.prepare import build_packet, placeholders
            transition(c, app_id, "PREPARING", "user", "SOP edited by user")
            build_packet(c, s, app_id, sop_override=d["text"])
            transition(c, app_id, "READY_TO_FILL", "applicant", "packet rebuilt with your SOP")
            left = placeholders(d["text"])
            return {"ok": True, "placeholders_left": len(left)}
        if action == "fill":
            mode = d.get("mode", "simulate")
            if mode == "simulate":
                try:
                    return {"fill_run": worker.fill(c, s, app_id, worker.SimulatedPortal(c, app_id))}
                except worker.NeedsUser as e:
                    return {"error": str(e)}
            from validateagain_applay.prepare import placeholders
            p = one(c, "SELECT sop_text FROM packet WHERE application_id=? ORDER BY version DESC LIMIT 1", (app_id,))
            if p and placeholders(p["sop_text"]):
                return {"error": "the SOP still has unfinished placeholders; edit it first"}
            return run_bg(s, f"worker-{app_id}", lambda conn: worker.run_worker(conn, s, app_id, "playwright",
                                                                                 bool(d.get("headless"))))
        if action == "submit":
            try:
                return worker.submit(c, s, app_id, worker.SimulatedPortal(c, app_id))
            except worker.NeedsUser as e:
                return {"submitted": False, "error": str(e)}
        if action == "sim-edit":
            worker.SimulatedPortal(c, app_id).user_edit(d["field"], d["value"])
            return {"ok": True, "note": "simulated: you changed a field in the portal after the screenshots"}
        if action == "portal":
            with tx(c):
                c.execute("UPDATE application SET portal_url=? WHERE id=?", (d["url"], app_id))
            return {"ok": True}
        if action == "retry":
            transition(c, app_id, "READY_TO_FILL", "user", "retry from app")
            return {"ok": True}
        if action == "rebuild":
            transition(c, app_id, "PREPARING", "user", "rebuild packet")
            from validateagain_applay.agent import prepare_ready
            return {"ok": True, "prepared": prepare_ready(c, s)}
        if action == "verify":
            with tx(c):
                c.execute("""UPDATE opportunity SET verified_at=datetime('now'), verified_by='user' WHERE id=
                             (SELECT opportunity_id FROM application WHERE id=?)""", (app_id,))
            return {"ok": True}
        if action == "withdraw":
            transition(c, app_id, "WITHDRAWN", "user", "withdrawn in app")
            return {"ok": True}
        if action == "rerank":
            st = one(c, "SELECT status FROM application WHERE id=?", (app_id,))["status"]
            if st in ("SKIPPED", "INELIGIBLE", "BELOW_THRESHOLD"):
                transition(c, app_id, "SHORTLISTED" if st == "SKIPPED" else "DISCOVERED", "user", "re-rank requested")
            return {"ok": True}
        return {"error": "unknown action"}

    # -- demo portal
    def portal_get(self, path: str):
        if path in ("/demo-portal", "/demo-portal/", "/demo-portal/start"):
            return self._send(200, portal_page(0).encode(), "text/html; charset=utf-8")
        if path.startswith("/demo-portal/page/"):
            return self._send(200, portal_page(int(path.rsplit("/", 1)[1])).encode(), "text/html; charset=utf-8")
        if path == "/demo-portal/confirmation":
            ref = PORTAL_STATE.get("submitted") or "none"
            return self._send(200, (f"<html><body><h1 id='confirmation'>Application submitted</h1>"
                                    f"<p>Reference: <b id='ref'>{ref}</b></p></body></html>").encode(), "text/html")
        return self._send(404, b"not found", "text/plain")

    def portal_post(self, path: str):
        d = self.body()
        if path.startswith("/demo-portal/step/"):
            step = int(path.rsplit("/", 1)[1])
            for f, _ in PORTAL_PAGES[step][2]:
                PORTAL_STATE["values"][f] = d.get(f, "")
            return self._send(303, b"", "text/plain", {"Location": f"/demo-portal/page/{step + 1}"})
        if path == "/demo-portal/submit":
            if not d.get("declare"):
                return self._send(400, b"declaration not accepted", "text/plain")
            PORTAL_STATE["submitted"] = "DEMO-" + secrets.token_hex(3).upper()
            return self._send(303, b"", "text/plain", {"Location": "/demo-portal/confirmation"})
        return self._send(404, b"not found", "text/plain")


def serve(settings: Settings | None = None, port: int = 8765, dev_approvals: bool = False, token: str | None = None,
          block: bool = True):
    settings = settings or load_settings()
    host = settings["app"].get("host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("the app only binds to localhost")
    tok = token or secrets.token_urlsafe(24)
    handler = type("H", (Handler,), {"settings": settings, "token": tok, "dev": dev_approvals,
                                     "conn": connect(settings)})
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Scholarship tracker app: http://{host}:{httpd.server_address[1]}/?token={tok}")
    if dev_approvals:
        print("DEV APPROVALS ON: approvals can be decided in the app with your PIN (local testing only).")
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return httpd, tok
