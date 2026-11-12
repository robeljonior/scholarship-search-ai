"""Paths and settings. Stdlib only, works on Python 3.10.

TRACKER_HOME (default: ./data next to this package) holds the database, vault,
packets, evidence and logs. Settings come from TRACKER_HOME/config.json; every key
has a default so a fresh install runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    # "live" fetches the real web; "demo" serves demo/pages fixtures for local testing.
    "fetch_mode": "live",
    # "gmail" uses the Gmail API; "demo" reads demo/inbox.json.
    "mail_source": "gmail",
    "ollama_host": "http://127.0.0.1:11434",
    "models": {
        "extract": "qwen2.5:7b-instruct",
        "classify": "qwen2.5:3b-instruct",
        "draft": "qwen2.5:7b-instruct",
    },
    "use_llm": True,
    "shortlist_threshold": 60,
    "max_apply_requests_per_run": 5,
    "min_prep_days": 7,
    "weights_version": "w2",
    "weights": {"funding": 30, "fit": 30, "readiness": 20, "location": 20, "competitiveness": 0},
    "region_preference": ["Europe", "Canada", "Australia", "USA"],
    "approval_ttl_hours": {"APPLY": 168, "SUBMIT": 24, "STATUS": 168, "REPLY": 72},
    "telegram": {"bot_token": "", "chat_id": ""},
    "app": {"host": "127.0.0.1", "port": 8765, "dev_approvals": False},
    "reminder_days": [60, 30, 14, 7, 1],
    "max_catalog_links": 20,
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Settings:
    home: Path
    data: dict = field(default_factory=dict)

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def db_path(self) -> Path:
        return self.home / "tracker.db"

    @property
    def vault_dir(self) -> Path:
        return self.home / "vault"

    @property
    def packets_dir(self) -> Path:
        return self.home / "packets"

    @property
    def evidence_dir(self) -> Path:
        return self.home / "evidence"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def browser_profile_dir(self) -> Path:
        return self.home / "browser_profile"


def tracker_home() -> Path:
    env = os.environ.get("TRACKER_HOME")
    return Path(env).expanduser().resolve() if env else PACKAGE_ROOT / "data"


def load_settings(home: Path | None = None) -> Settings:
    home = home or tracker_home()
    data = dict(DEFAULTS)
    cfg = home / "config.json"
    if cfg.exists():
        data = _deep_merge(data, json.loads(cfg.read_text()))
    return Settings(home=home, data=data)


def save_settings(settings: Settings, overrides: dict) -> None:
    cfg = settings.home / "config.json"
    current = json.loads(cfg.read_text()) if cfg.exists() else {}
    current = _deep_merge(current, overrides)
    cfg.write_text(json.dumps(current, indent=2))
    settings.data = _deep_merge(settings.data, overrides)


def now() -> datetime:
    """Current UTC time. TRACKER_NOW (ISO) overrides it for tests and demos."""
    fixed = os.environ.get("TRACKER_NOW")
    if fixed:
        dt = datetime.fromisoformat(fixed)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).isoformat(timespec="seconds")
