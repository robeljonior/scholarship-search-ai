"""Mail sources. Bodies stay in memory for local classification and are never stored.

GmailMailbox  Gmail API. Needs `pip install google-api-python-client google-auth-oauthlib`
              and an OAuth client file at vault/gmail_client_secret.json. `tracker mail auth`
              runs the loopback flow (readonly; `--send` adds gmail.send for approved replies).
DemoMailbox   demo/inbox.json, for local testing. "Sent" replies go to logs/sent_mail.log.
"""
from __future__ import annotations

import base64
import email.utils
import json
import os
from datetime import timedelta
from email.mime.text import MIMEText

from shared.config import PACKAGE_ROOT, Settings, iso, now
from shared.db import kv_get, kv_set, tx

SCOPES_READ = ["https://www.googleapis.com/auth/gmail.readonly"]
SCOPES_SEND = SCOPES_READ + ["https://www.googleapis.com/auth/gmail.send"]


class MailError(Exception):
    pass


def _addr(header: str) -> str:
    return email.utils.parseaddr(header or "")[1].lower()


class DemoMailbox:
    can_send = True

    def __init__(self, settings: Settings, path=None):
        self.settings = settings
        self.path = path or (PACKAGE_ROOT / "demo" / "inbox.json")

    def fetch_new(self, conn) -> list[dict]:
        msgs = json.loads(self.path.read_text())
        extra = kv_get(conn, "demo_inbox_extra", [])
        return msgs + extra

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None, thread_id: str | None) -> str:
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(self.settings.logs_dir / "sent_mail.log", "a") as f:
            f.write(f"--- {iso()}\nTo: {to}\nSubject: {subject}\nIn-Reply-To: {in_reply_to}\n\n{body}\n")
        return "demo-sent"


class GmailMailbox:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.token_path = settings.vault_dir / "gmail_token.json"
        if not self.token_path.exists():
            raise MailError("Gmail not authorised; run `tracker mail auth`")
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as e:
            raise MailError("pip install google-api-python-client google-auth-oauthlib") from e
        creds = Credentials.from_authorized_user_file(str(self.token_path))
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json())
            os.chmod(self.token_path, 0o600)
        self.can_send = "https://www.googleapis.com/auth/gmail.send" in (creds.scopes or [])
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch_new(self, conn) -> list[dict]:
        last = kv_get(conn, "gmail_last_sync")
        # overlap one day; duplicates are removed by gmail_id UNIQUE
        after = (now() - timedelta(days=30)) if not last else (now().fromisoformat(last) - timedelta(days=1))
        q = f"after:{int(after.timestamp())} -category:promotions -category:social"
        out, token = [], None
        while True:
            resp = self.svc.users().messages().list(userId="me", q=q, pageToken=token, maxResults=100).execute()
            for m in resp.get("messages", []):
                if conn.execute("SELECT 1 FROM email_record WHERE gmail_id=?", (m["id"],)).fetchone():
                    continue
                full = self.svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
                out.append(self._parse(full))
            token = resp.get("nextPageToken")
            if not token or len(out) > 500:
                break
        with tx(conn):
            kv_set(conn, "gmail_last_sync", iso())
        return out

    @staticmethod
    def _parse(full: dict) -> dict:
        headers = {h["name"].lower(): h["value"] for h in full["payload"].get("headers", [])}

        def body_of(part) -> str:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
            for p in part.get("parts", []) or []:
                b = body_of(p)
                if b:
                    return b
            return ""
        return {"id": full["id"], "threadId": full.get("threadId"), "from": headers.get("from", ""),
                "reply_to": headers.get("reply-to", ""), "subject": headers.get("subject", ""),
                "date": headers.get("date", ""), "message_id": headers.get("message-id", ""),
                "body": body_of(full["payload"]) or full.get("snippet", "")}

    def send(self, to: str, subject: str, body: str, in_reply_to: str | None, thread_id: str | None) -> str:
        if not self.can_send:
            raise MailError("sending not authorised; run `tracker mail auth --send`")
        msg = MIMEText(body)
        msg["To"], msg["Subject"] = to, subject
        if in_reply_to:
            msg["In-Reply-To"] = msg["References"] = in_reply_to
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        body_ = {"raw": raw}
        if thread_id:
            body_["threadId"] = thread_id
        return self.svc.users().messages().send(userId="me", body=body_).execute()["id"]


def authorise(settings: Settings, send: bool = False) -> str:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise MailError("pip install google-api-python-client google-auth-oauthlib") from e
    secret = settings.vault_dir / "gmail_client_secret.json"
    if not secret.exists():
        raise MailError(f"put your OAuth desktop client JSON at {secret}")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES_SEND if send else SCOPES_READ)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    p = settings.vault_dir / "gmail_token.json"
    p.write_text(creds.to_json())
    os.chmod(p, 0o600)
    return str(p)


def get_mailbox(settings: Settings):
    return DemoMailbox(settings) if settings.get("mail_source") == "demo" else GmailMailbox(settings)


sender_address = _addr
