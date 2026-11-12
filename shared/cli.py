"""`tracker` command line. Every agent-facing command supports --json.

Commands the OpenClaw skills may call are listed in skills/*/SKILL.md. The commands in
USER_ONLY refuse to run without a terminal and with --json, so no agent can use them.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys

from . import approvals, vault
from .config import PACKAGE_ROOT, load_settings, save_settings
from .db import connect, rows, tx

USER_ONLY = {"approve", "approvals set-pin", "bot setup", "mail auth", "mail reply-edit", "worker run"}


def out(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, list):
        for r in data:
            print(json.dumps(r, default=str) if not isinstance(r, str) else r)
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}")
    else:
        print(data)


def require_tty(args, name: str) -> None:
    if getattr(args, "json", False) or not sys.stdin.isatty():
        sys.exit(f"`{name}` is user-only: run it yourself in a terminal (no --json, needs a TTY).")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="tracker", description="AI scholarship tracker (phase 2)")
    p.add_argument("--json", action="store_true", help="JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create data dir, database, vault and seed sources")
    d = sub.add_parser("demo", help="set up an offline demo (fixture pages + demo inbox) in a separate TRACKER_HOME")
    d.add_argument("--pin", help="set the approval PIN (demo only; otherwise use `approvals set-pin`)")
    sub.add_parser("health")

    v = sub.add_parser("vault").add_subparsers(dest="sub", required=True)
    v.add_parser("check")
    v.add_parser("summary")

    ap = sub.add_parser("approvals").add_subparsers(dest="sub", required=True)
    ap.add_parser("list")
    ap.add_parser("set-pin")
    a = sub.add_parser("approve", help="USER ONLY: approve or reject in the terminal with your PIN")
    a.add_argument("id", type=int)
    a.add_argument("--reject", action="store_true")

    ds = sub.add_parser("discover").add_subparsers(dest="sub", required=True)
    ds.add_parser("sources")
    ds.add_parser("coverage")
    ds.add_parser("seed")
    x = ds.add_parser("add-source")
    x.add_argument("url")
    x.add_argument("--tier", default="web", choices=["catalog", "university", "country", "web", "lead"])
    x.add_argument("--name")
    x = ds.add_parser("propose-url")
    x.add_argument("url")
    x = ds.add_parser("leads")
    x = ds.add_parser("candidates")
    x.add_argument("action", choices=["list", "accept", "reject", "show"])
    x.add_argument("id", nargs="?", type=int)
    x.add_argument("--status", default="needs_user_review")

    ag = sub.add_parser("agent").add_subparsers(dest="sub", required=True)
    x = ag.add_parser("scout")
    x.add_argument("--all", action="store_true", help="check all sources, not only due ones")
    ag.add_parser("applicant")
    ag.add_parser("monitor")
    ag.add_parser("status")
    x = ag.add_parser("fill", help="simulated fill (real portals: `worker run`)")
    x.add_argument("id", type=int)
    x = ag.add_parser("submit", help="simulated submit after approval #2")
    x.add_argument("id", type=int)

    w = sub.add_parser("worker").add_subparsers(dest="sub", required=True)
    x = w.add_parser("run", help="USER ONLY: real browser fill -> wait for approval #2 -> submit")
    x.add_argument("id", type=int)
    x.add_argument("--headless", action="store_true")

    ap2 = sub.add_parser("application").add_subparsers(dest="sub", required=True)
    ap2.add_parser("list")
    x = ap2.add_parser("show")
    x.add_argument("id", type=int)
    x = ap2.add_parser("portal")
    x.add_argument("id", type=int)
    x.add_argument("url")
    x = ap2.add_parser("retry", help="NEEDS_USER/READY -> rebuild or refill")
    x.add_argument("id", type=int)
    x = ap2.add_parser("withdraw")
    x.add_argument("id", type=int)

    m = sub.add_parser("mail").add_subparsers(dest="sub", required=True)
    x = m.add_parser("auth")
    x.add_argument("--send", action="store_true")
    m.add_parser("list")
    m.add_parser("replies")
    x = m.add_parser("reply-edit")
    x.add_argument("id", type=int)

    e = sub.add_parser("events").add_subparsers(dest="sub", required=True)
    e.add_parser("list")
    e.add_parser("dispatch")
    sub.add_parser("jobs").add_subparsers(dest="sub", required=True).add_parser("run-due")
    b = sub.add_parser("backup").add_subparsers(dest="sub", required=True)
    b.add_parser("create")
    x = b.add_parser("restore-test")
    x.add_argument("archive")

    x = sub.add_parser("app", help="local web app on 127.0.0.1")
    x.add_argument("--port", type=int)
    x.add_argument("--dev-approvals", action="store_true",
                   help="allow approving in the web app with the PIN (local testing only)")
    bt = sub.add_parser("bot").add_subparsers(dest="sub", required=True)
    bt.add_parser("run")
    bt.add_parser("setup")

    args = p.parse_args(argv)
    run(args)


def run(args) -> None:
    settings = load_settings()
    J = args.json

    if args.cmd == "init":
        from search_find_validate import registry
        settings.home.mkdir(parents=True, exist_ok=True)
        conn = connect(settings)
        vault.init_vault(settings)
        registry.seed(conn)
        cfg = settings.home / "config.json"
        if not cfg.exists():
            shutil.copy(PACKAGE_ROOT / "config.example.json", cfg)
        out({"home": str(settings.home), "vault": str(settings.vault_dir),
             "next": ["edit vault/profile.json", "tracker approvals set-pin", "tracker app"]}, J)
        return

    if args.cmd == "demo":
        from demo.seed import setup_demo
        out(setup_demo(settings, args.pin), J)
        return

    conn = connect(settings)

    if args.cmd == "health":
        from .health import health
        out(health(conn, settings), J)
    elif args.cmd == "vault":
        if args.sub == "check":
            out({"problems": vault.check_permissions(settings)}, J)
        else:
            out(vault.profile_summary(vault.load_profile(settings)), J)
    elif args.cmd == "approvals":
        if args.sub == "list":
            out(approvals.pending(conn), J)
        else:
            require_tty(args, "approvals set-pin")
            pin = getpass.getpass("New approval PIN (4-12 digits): ")
            if pin != getpass.getpass("Repeat PIN: "):
                sys.exit("PINs differ")
            vault.set_pin(settings, pin)
            print("PIN set.")
    elif args.cmd == "approve":
        require_tty(args, "approve")
        a = conn.execute("SELECT * FROM approval WHERE id=?", (args.id,)).fetchone()
        if not a:
            sys.exit("no such approval")
        print(f"[{a['action_type']} #{a['id']}]\n{a['summary']}\n")
        pin = getpass.getpass(("REJECT" if args.reject else "APPROVE") + " - enter PIN: ")
        try:
            from app.server import after_decision
            out(after_decision(conn, settings, approvals.decide(conn, settings, args.id, not args.reject, pin, "terminal")), False)
        except approvals.ApprovalError as e:
            sys.exit(f"refused: {e}")
    elif args.cmd == "discover":
        _discover(conn, settings, args, J)
    elif args.cmd == "agent":
        _agent(conn, settings, args, J)
    elif args.cmd == "worker":
        require_tty(args, "worker run")
        from validateagain_applay.worker import run_worker
        out(run_worker(conn, settings, args.id, "playwright", args.headless), J)
    elif args.cmd == "application":
        _application(conn, settings, args, J)
    elif args.cmd == "mail":
        _mail(conn, settings, args, J)
    elif args.cmd == "events":
        from .jobs import dispatch
        if args.sub == "list":
            out(rows(conn, "SELECT id, event_type, payload_json, created_at, delivered_at, attempts, last_error FROM event_outbox "
                           "ORDER BY id DESC LIMIT 50"), J)
        else:
            out(dispatch(conn, settings), J)
    elif args.cmd == "jobs":
        from .jobs import job, run_due
        with job(conn, "run-due") as s:
            s.update(run_due(conn, settings))
        out(s, J)
    elif args.cmd == "backup":
        from .health import backup, restore_test
        out({"archive": backup(conn, settings)} if args.sub == "create" else restore_test(conn, settings, args.archive), J)
    elif args.cmd == "app":
        from app.server import serve
        dev = args.dev_approvals or settings["app"].get("dev_approvals", False)
        serve(settings, port=args.port or settings["app"]["port"], dev_approvals=dev)
    elif args.cmd == "bot":
        if args.sub == "setup":
            require_tty(args, "bot setup")
            token = getpass.getpass("Approval bot token from @BotFather (NOT OpenClaw's bot): ").strip()
            chat = input("Your Telegram chat id (send /start to the bot, then check getUpdates): ").strip()
            save_settings(settings, {"telegram": {"bot_token": token, "chat_id": chat}})
            import os
            os.chmod(settings.home / "config.json", 0o600)
            print("saved to data/config.json (600)")
        else:
            from .approval_bot import Bot
            Bot(conn, settings).run()


def _discover(conn, settings, args, J):
    from search_find_validate import agent as scout, registry
    if args.sub == "sources":
        out(rows(conn, "SELECT id, name, tier, url, last_checked_at, status FROM source ORDER BY id"), J)
    elif args.sub == "coverage":
        out(registry.coverage(conn), J)
    elif args.sub == "seed":
        out({"seeded": registry.seed(conn)}, J)
    elif args.sub == "add-source":
        out({"source_id": registry.add_source(conn, args.url, args.tier, args.name)}, J)
    elif args.sub == "propose-url":
        out(scout.propose_url(conn, settings, args.url), J)
    elif args.sub == "leads":
        out(rows(conn, "SELECT id, classification, status, drop_reason, official_url, substr(excerpt,1,120) AS excerpt "
                       "FROM lead ORDER BY id DESC LIMIT 50"), J)
    elif args.sub == "candidates":
        if args.action == "list":
            out(rows(conn, "SELECT id, url, review_status, review_reasons, json_extract(extracted_json,'$.name') AS name,"
                           " json_extract(extracted_json,'$.deadline') AS deadline FROM candidate WHERE review_status=? "
                           "ORDER BY id DESC", (args.status,)), J)
        elif args.action == "show":
            out(rows(conn, "SELECT * FROM candidate WHERE id=?", (args.id,)), J)
        elif args.action == "accept":
            out({"opportunity_id": scout.accept_candidate(conn, args.id, "user")}, J)
        else:
            scout.reject_candidate(conn, args.id)
            out({"rejected": args.id}, J)


def _agent(conn, settings, args, J):
    if args.sub == "scout":
        from search_find_validate.agent import run_scout
        out(run_scout(conn, settings, only_due=not args.all), J)
    elif args.sub == "applicant":
        from validateagain_applay.agent import run_applicant
        out(run_applicant(conn, settings), J)
    elif args.sub == "monitor":
        from track_response.agent import run_monitor
        out(run_monitor(conn, settings), J)
    elif args.sub == "status":
        out(agent_status(conn), J)
    elif args.sub == "fill":
        from validateagain_applay.worker import SimulatedPortal, fill
        out({"fill_run": fill(conn, settings, args.id, SimulatedPortal(conn, args.id))}, J)
    elif args.sub == "submit":
        from validateagain_applay.worker import NeedsUser, SimulatedPortal, submit
        try:
            out(submit(conn, settings, args.id, SimulatedPortal(conn, args.id)), J)
        except NeedsUser as e:
            out({"submitted": False, "error": str(e)}, J)


def agent_status(conn) -> dict:
    last = {}
    for name in ("scout", "applicant", "monitor", "run-due"):
        r = conn.execute("SELECT finished_at, outcome, summary_json FROM job_run WHERE job_name=? ORDER BY id DESC LIMIT 1",
                         (name,)).fetchone()
        last[name] = dict(r) if r else None
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM application GROUP BY status")}
    return {"last_runs": last, "applications_by_status": counts,
            "approvals_pending": conn.execute("SELECT COUNT(*) FROM approval WHERE decision='pending'").fetchone()[0],
            "candidates_needing_review": conn.execute(
                "SELECT COUNT(*) FROM candidate WHERE review_status='needs_user_review'").fetchone()[0]}


def _application(conn, settings, args, J):
    from .state_machine import transition
    if args.sub == "list":
        out(rows(conn, """SELECT a.id, a.status, a.eligibility_class, a.score, o.name, o.deadline FROM application a
                          JOIN opportunity o ON o.id=a.opportunity_id ORDER BY a.score DESC"""), J)
    elif args.sub == "show":
        from app.server import application_detail
        out(application_detail(conn, args.id), J)
    elif args.sub == "portal":
        with tx(conn):
            conn.execute("UPDATE application SET portal_url=? WHERE id=?", (args.url, args.id))
        out({"ok": True}, J)
    elif args.sub == "retry":
        st = conn.execute("SELECT status FROM application WHERE id=?", (args.id,)).fetchone()["status"]
        transition(conn, args.id, "READY_TO_FILL", "user", "retry")
        out({"from": st, "to": "READY_TO_FILL"}, J)
    elif args.sub == "withdraw":
        transition(conn, args.id, "WITHDRAWN", "user", "withdrawn by user")
        out({"ok": True}, J)


def _mail(conn, settings, args, J):
    if args.sub == "auth":
        require_tty(args, "mail auth")
        from track_response.mailbox import authorise
        out({"token": authorise(settings, args.send)}, J)
    elif args.sub == "list":
        out(rows(conn, "SELECT id, received_at, from_addr, subject, category, application_id, suspicious FROM email_record "
                       "ORDER BY id DESC LIMIT 50"), J)
    elif args.sub == "replies":
        out(rows(conn, "SELECT id, recipient, subject, status, created_at FROM reply_draft ORDER BY id DESC"), J)
    elif args.sub == "reply-edit":
        require_tty(args, "mail reply-edit")
        from track_response.replies import edit_draft
        print("Type the new reply. End with a line containing only '.'")
        lines = []
        while (line := input()) != ".":
            lines.append(line)
        out({"approval_id": edit_draft(conn, settings, args.id, "\n".join(lines))}, J)


if __name__ == "__main__":
    main()
