"""End-to-end and security tests. Stdlib only:  python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from shared import approvals, vault
from shared.config import load_settings
from shared.db import connect, one, rows, tx
from shared.fetcher import FetchError, check_url
from shared.state_machine import TransitionError, transition


def demo_env():
    tmp = tempfile.mkdtemp(prefix="tracker-test-")
    os.environ["TRACKER_HOME"] = tmp
    from demo.seed import setup_demo
    setup_demo(load_settings(Path(tmp)), "1234")
    s = load_settings(Path(tmp))
    s.data["use_llm"] = False  # deterministic tests, no Ollama needed
    return s, connect(s)


class NoLLM:
    def available(self):
        return False


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.s, self.c = demo_env()
        from search_find_validate.agent import run_scout
        from validateagain_applay.agent import run_applicant
        self.scout = run_scout(self.c, self.s, only_due=False, llm=NoLLM())
        self.applicant = run_applicant(self.c, self.s, llm=NoLLM())

    def app_id(self, name_part):
        return one(self.c, "SELECT a.id FROM application a JOIN opportunity o ON o.id=a.opportunity_id WHERE o.name LIKE ?",
                   (f"%{name_part}%",))["id"]

    def test_scout_fake_check_and_leads(self):
        self.assertEqual(self.scout["accepted"], 5)
        self.assertEqual(self.scout["needs_review"], 2)  # aggregator domain + passed deadline
        dropped = {r["classification"] for r in rows(self.c, "SELECT classification FROM lead WHERE status='dropped'")}
        self.assertEqual(dropped, {"advert", "scam_suspect", "self_funded"})
        bach = one(self.c, "SELECT review_status FROM candidate WHERE url LIKE '%bachelor%'")
        self.assertEqual(bach["review_status"], "invalid")

    def test_rescan_creates_no_duplicates(self):
        from search_find_validate.agent import run_scout
        before = self.c.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
        r = run_scout(self.c, self.s, only_due=False, llm=NoLLM())
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM candidate").fetchone()[0], before)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM candidate WHERE review_status='needs_user_review'").fetchone()[0], 2)
        self.assertGreater(r["unchanged"], 0)

    def test_submit_approval_refuses_placeholders(self):
        from app.server import after_decision
        from validateagain_applay import worker
        aid = self.app_id("Joint Master in Artificial")
        after_decision(self.c, self.s, self._approve("APPLY", aid))
        portal = worker.SimulatedPortal(self.c, aid)
        from validateagain_applay.prepare import build_packet
        transition(self.c, aid, "PREPARING", "user", "x")
        build_packet(self.c, self.s, aid, sop_override="Done.")
        transition(self.c, aid, "READY_TO_FILL", "applicant", "x")
        worker.fill(self.c, self.s, aid, portal)
        # user types a placeholder into the portal and the form is re-captured
        portal.user_edit("sop", "Intro [DESCRIBE YOUR RESEARCH PROJECT HERE]")
        self._approve("SUBMIT")
        worker.submit(self.c, self.s, aid, portal)  # blocked: form changed -> new approval with the placeholder
        with self.assertRaisesRegex(approvals.ApprovalError, "placeholder"):
            self._approve("SUBMIT")

    def test_cannot_accept_expired_candidate(self):
        from search_find_validate.agent import accept_candidate
        cid = one(self.c, "SELECT id FROM candidate WHERE url LIKE '%oldtown%'")["id"]
        with self.assertRaisesRegex(ValueError, "deadline already passed"):
            accept_candidate(self.c, cid, "user")

    def test_ranking(self):
        ds = one(self.c, "SELECT * FROM application WHERE id=?", (self.app_id("Data Science"),))
        self.assertEqual(ds["status"], "BELOW_THRESHOLD")  # IELTS 7.5 needed, profile has 7.0 (unverified)
        ai = one(self.c, "SELECT * FROM application WHERE id=?", (self.app_id("Joint Master in Artificial"),))
        self.assertEqual(ai["status"], "AWAITING_APPLY")
        self.assertEqual(ai["eligibility_class"], "ELIGIBLE")

    def test_profile_change_rechecks_every_application(self):
        from validateagain_applay.agent import run_applicant
        ai = self.app_id("Joint Master in Artificial")
        pend = [x for x in approvals.pending(self.c) if x["action_type"] == "APPLY"]
        self.assertTrue(pend)
        # unchanged profile: nothing is reset, approvals stay
        r = run_applicant(self.c, self.s, llm=NoLLM())
        self.assertEqual(r["reset_after_profile_change"], [])
        self.assertEqual(len([x for x in approvals.pending(self.c) if x["action_type"] == "APPLY"]), len(pend))
        prof = vault.load_profile(self.s)
        prof["degree"]["gpa"] = 2.78
        prof["english"]["score"] = 5.5
        vault_path = [q for q in Path(os.environ["TRACKER_HOME"]).rglob("profile.json")][0]
        vault_path.write_text(json.dumps(prof))
        r = run_applicant(self.c, self.s, llm=NoLLM())
        self.assertIn(ai, r["reset_after_profile_change"])
        self.assertEqual(one(self.c, "SELECT status FROM application WHERE id=?", (ai,))["status"], "BELOW_THRESHOLD")
        self.assertEqual([x for x in approvals.pending(self.c) if x["action_type"] == "APPLY"], [])
        with self.assertRaises(approvals.ApprovalError):
            approvals.decide(self.c, self.s, pend[0]["id"], True, "1234", "app-dev")

    def _approve(self, typ, resource=None, pin="1234"):
        a = [x for x in approvals.pending(self.c) if x["action_type"] == typ and (resource is None or x["resource_id"] == resource)][0]
        return approvals.decide(self.c, self.s, a["id"], True, pin, "app-dev")

    def test_full_flow_with_tamper_check(self):
        from app.server import after_decision
        from validateagain_applay import worker
        from track_response.agent import run_monitor
        aid = self.app_id("Joint Master in Artificial")
        res = after_decision(self.c, self.s, self._approve("APPLY", aid))
        self.assertEqual(res["prepared"], [aid])
        portal = worker.SimulatedPortal(self.c, aid)
        with self.assertRaisesRegex(worker.NeedsUser, "placeholder"):  # SOP draft not finished yet
            worker.fill(self.c, self.s, aid, portal)
        from validateagain_applay.prepare import build_packet
        transition(self.c, aid, "PREPARING", "user", "SOP edited")
        build_packet(self.c, self.s, aid, sop_override="My finished statement of purpose.")
        transition(self.c, aid, "READY_TO_FILL", "applicant", "rebuilt")
        worker.fill(self.c, self.s, aid, portal)
        self.assertEqual(one(self.c, "SELECT status FROM application WHERE id=?", (aid,))["status"], "AWAITING_SUBMIT")
        # cannot submit before approval #2
        with self.assertRaises(worker.NeedsUser):
            worker.submit(self.c, self.s, aid, portal)
        self._approve("SUBMIT")
        portal.user_edit("gpa", "4.0")  # form changed after the screenshots
        r = worker.submit(self.c, self.s, aid, portal)
        self.assertFalse(r["submitted"])
        self.assertEqual(one(self.c, "SELECT status FROM application WHERE id=?", (aid,))["status"], "AWAITING_SUBMIT")
        self._approve("SUBMIT")
        r = worker.submit(self.c, self.s, aid, portal)
        self.assertTrue(r["submitted"])
        self.assertTrue(one(self.c, "SELECT 1 AS x FROM evidence WHERE application_id=? AND kind='confirmation'", (aid,)))
        # monitor: proposals only, no direct status change
        m = run_monitor(self.c, self.s, llm=NoLLM())
        self.assertEqual(m["status_proposals"], 1)
        self.assertEqual(m["reply_drafts"], 1)
        self.assertEqual(one(self.c, "SELECT status FROM application WHERE id=?", (aid,))["status"], "SUBMITTED")
        phish = one(self.c, "SELECT * FROM email_record WHERE gmail_id='demo-3'")
        self.assertEqual(phish["suspicious"], 1)
        self.assertIsNone(phish["application_id"])
        self._approve("STATUS")
        self.assertEqual(one(self.c, "SELECT status FROM application WHERE id=?", (aid,))["status"], "INTERVIEW")
        res = self._approve("REPLY")
        self.assertTrue(res["sent"])
        self.assertEqual(one(self.c, "SELECT status FROM reply_draft")["status"], "sent")

    def test_apply_invalidated_when_deadline_changes(self):
        aid = self.app_id("Joint Master in Artificial")
        with tx(self.c):
            self.c.execute("UPDATE opportunity SET deadline='2027-01-10' WHERE id=(SELECT opportunity_id FROM application WHERE id=?)", (aid,))
        res = self._approve("APPLY", aid)
        self.assertEqual(res["result"], "invalidated")


class Approvals(unittest.TestCase):
    def setUp(self):
        self.s, self.c = demo_env()
        from search_find_validate.agent import run_scout
        from validateagain_applay.agent import run_applicant
        run_scout(self.c, self.s, only_due=False, llm=NoLLM())
        run_applicant(self.c, self.s, llm=NoLLM())
        self.a = approvals.pending(self.c)[0]

    def test_wrong_pin_and_lockout(self):
        for _ in range(approvals.MAX_PIN_FAILURES):
            with self.assertRaises(approvals.ApprovalError):
                approvals.decide(self.c, self.s, self.a["id"], True, "0000", "app-dev")
        with self.assertRaisesRegex(approvals.ApprovalError, "locked"):
            approvals.decide(self.c, self.s, self.a["id"], True, "1234", "app-dev")

    def test_replay(self):
        approvals.decide(self.c, self.s, self.a["id"], True, "1234", "app-dev")
        with self.assertRaisesRegex(approvals.ApprovalError, "already"):
            approvals.decide(self.c, self.s, self.a["id"], True, "1234", "app-dev")

    def test_expired(self):
        with tx(self.c):
            self.c.execute("UPDATE approval SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (self.a["id"],))
        self.assertEqual(approvals.decide(self.c, self.s, self.a["id"], True, "1234", "app-dev")["result"], "expired")

    def test_telegram_wrong_chat(self):
        self.s.data["telegram"] = {"bot_token": "x", "chat_id": "42"}
        with self.assertRaisesRegex(approvals.ApprovalError, "chat"):
            approvals.decide(self.c, self.s, self.a["id"], True, "1234", "telegram", chat_id="99")

    def test_classifier_cannot_write_status(self):
        app_id = self.a["resource_id"]
        with self.assertRaises(TransitionError):
            transition(self.c, app_id, "SUBMITTED", "monitor")
        with self.assertRaises(TransitionError):
            transition(self.c, app_id, "SUBMITTING", "applicant")  # needs approval #2

    def test_cli_approve_refuses_json(self):
        from shared import cli
        with self.assertRaises(SystemExit):
            cli.main(["--json", "approve", str(self.a["id"])])


class Fetcher(unittest.TestCase):
    def test_ssrf(self):
        for url in ["file:///etc/passwd", "http://127.0.0.1/", "http://localhost:8080", "http://10.0.0.5/",
                    "http://169.254.169.254/latest/meta-data", "http://user:pw@example.com/", "ftp://example.com"]:
            with self.assertRaises(FetchError, msg=url):
                check_url(url)


class Units(unittest.TestCase):
    def test_extract_deadline_and_requirements(self):
        from search_find_validate.extract import rule_extract
        d = rule_extract("Master of Science in AI. Fully funded. Minimum GPA of 3.0 out of 4.0. IELTS overall 6.5. "
                         "Application deadline: 15 January 2027, 23:59 CET.", "MSc AI")
        self.assertEqual(d["deadline"], "2027-01-15")
        self.assertEqual(d["deadline_tz"], "Europe/Berlin")
        self.assertEqual(d["funding_type"], "full")
        self.assertEqual(d["requirements"]["ielts"], 6.5)
        self.assertEqual(d["requirements"]["min_gpa"], 3.0)

    def test_scam_signals(self):
        from search_find_validate.fake_check import scam_signals
        self.assertTrue(scam_signals("Pay a processing fee of $50 to receive your scholarship"))
        self.assertTrue(scam_signals("guaranteed admission and visa"))
        self.assertFalse(scam_signals("The scholarship covers tuition. Apply at https://www.example.edu"))

    def test_reply_checks(self):
        from track_response.replies import check_text
        self.assertTrue(check_text("please pay via bank transfer"))
        self.assertTrue(check_text("see https://evil.example"))
        self.assertFalse(check_text("Thank you, I will attend the interview."))

    def test_eligibility_unknown_is_not_failure(self):
        from validateagain_applay.eligibility import check
        prof = json.loads(json.dumps(vault.SAMPLE_PROFILE))
        r = check(prof, {"deadline": "2099-01-01", "verified_at": None}, {})
        self.assertEqual(r["class"], "POTENTIALLY_ELIGIBLE")
        r = check(prof, {"deadline": "2099-01-01", "verified_at": "x"}, {"ielts": 8.0, "open_to_all": True})
        self.assertEqual(r["class"], "INELIGIBLE")


class AppServer(unittest.TestCase):
    def test_auth(self):
        import urllib.error
        import urllib.request
        s, c = demo_env()
        from app.server import serve
        httpd, tok = serve(s, port=0, token="t0k", block=False)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview")
            self.assertEqual(e.exception.code, 401)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/overview", headers={"Cookie": "tk=t0k"})
            self.assertIn("status", json.loads(urllib.request.urlopen(req).read()))
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/approvals/1/decide", data=b"{}", method="POST",
                                         headers={"Cookie": "tk=t0k", "Content-Type": "application/json", "X-Tracker-Token": "t0k"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)  # approvals disabled without --dev-approvals
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
