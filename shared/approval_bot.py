"""The tracker's OWN Telegram approval bot (separate token from OpenClaw's bot).

Flow: bot sends each pending approval with [Approve] [Reject] buttons -> user taps ->
bot asks for the PIN -> user sends PIN -> bot deletes the PIN message and calls
approvals.decide(via='telegram', chat_id=...). Only the configured chat id is accepted.

Run: `tracker bot run` (or the systemd unit tracker-approval-bot.service).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from . import approvals
from .config import Settings, iso
from .db import rows, tx


class Bot:
    def __init__(self, conn, settings: Settings):
        tg = settings["telegram"]
        if not tg.get("bot_token") or not tg.get("chat_id"):
            raise SystemExit("set telegram.bot_token and telegram.chat_id in data/config.json (use `tracker bot setup`)")
        self.conn, self.settings = conn, settings
        self.base = f"https://api.telegram.org/bot{tg['bot_token']}/"
        self.chat_id = str(tg["chat_id"])
        self.offset = 0
        self.awaiting_pin: dict | None = None  # {"approval_id", "approve", "asked_at"}

    def call(self, method: str, **params) -> dict:
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in params.items()}).encode()
        with urllib.request.urlopen(self.base + method, data=data, timeout=40) as r:
            return json.loads(r.read())

    def send_pending(self) -> None:
        for a in rows(self.conn, "SELECT * FROM approval WHERE decision='pending' AND notified_at IS NULL"):
            kb = {"inline_keyboard": [[{"text": "Approve", "callback_data": f"a:{a['id']}"},
                                       {"text": "Reject", "callback_data": f"r:{a['id']}"}]]}
            self.call("sendMessage", chat_id=self.chat_id, reply_markup=kb,
                      text=f"[{a['action_type']} #{a['id']}] expires {a['expires_at'][:16]}Z\n\n{a['summary']}"[:4000])
            with tx(self.conn):
                self.conn.execute("UPDATE approval SET notified_at=? WHERE id=?", (iso(), a["id"]))

    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            q = upd["callback_query"]
            chat = str(q["message"]["chat"]["id"])
            self.call("answerCallbackQuery", callback_query_id=q["id"])
            if chat != self.chat_id:
                return
            kind, aid = q["data"].split(":")
            self.awaiting_pin = {"approval_id": int(aid), "approve": kind == "a", "asked_at": time.time()}
            self.call("sendMessage", chat_id=self.chat_id,
                      text=f"{'Approve' if kind == 'a' else 'Reject'} #{aid}: send your PIN (it will be deleted).")
            return
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id"))
        text = (msg.get("text") or "").strip()
        if chat != self.chat_id or not self.awaiting_pin or not text.isdigit():
            return
        try:
            self.call("deleteMessage", chat_id=self.chat_id, message_id=msg["message_id"])
        except Exception:  # noqa: BLE001
            pass
        pend, self.awaiting_pin = self.awaiting_pin, None
        if time.time() - pend["asked_at"] > 300:
            self.call("sendMessage", chat_id=self.chat_id, text="PIN request timed out; tap the button again.")
            return
        try:
            res = approvals.decide(self.conn, self.settings, pend["approval_id"], pend["approve"], text, "telegram", chat)
            from app.server import after_decision
            res = after_decision(self.conn, self.settings, res)
            self.call("sendMessage", chat_id=self.chat_id, text=f"#{pend['approval_id']}: {res['result']}. "
                      f"{res.get('message', '')}")
        except approvals.ApprovalError as e:
            self.call("sendMessage", chat_id=self.chat_id, text=f"#{pend['approval_id']}: refused ({e})")

    def run(self) -> None:
        print(f"approval bot running for chat {self.chat_id}")
        while True:
            try:
                approvals.expire_due(self.conn)
                self.send_pending()
                resp = self.call("getUpdates", offset=self.offset, timeout=25,
                                 allowed_updates=["message", "callback_query"])
                for upd in resp.get("result", []):
                    self.offset = upd["update_id"] + 1
                    self.handle(upd)
            except Exception as e:  # noqa: BLE001 - keep the bot alive; errors are printed for journald
                print(f"bot error: {e}")
                time.sleep(10)
