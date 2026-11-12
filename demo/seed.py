"""Offline demo: fixture pages instead of the web, demo inbox instead of Gmail.

    TRACKER_HOME=data-demo ./tracker.sh demo --pin 1234
    TRACKER_HOME=data-demo ./tracker.sh app --dev-approvals
(or just ./demo.sh)
"""
from __future__ import annotations

import json

from search_find_validate import registry
from shared import vault
from shared.config import Settings, load_settings
from shared.db import connect

DEMO_SOURCES = [
    ("Erasmus Mundus catalogue (demo)", "https://www.eacea.ec.europa.eu/scholarships/erasmus-mundus-catalogue_en", "catalog"),
    ("Northlake University (demo)", "https://www.northlake.edu.au/scholarships/msc-machine-learning", "university"),
    ("Mapleview University (demo)", "https://gradstudies.mapleview.edu/funding/msc-computer-science", "university"),
    ("Telegram demo_scholarships (demo)", "https://t.me/s/demo_scholarships", "lead"),
    ("Scholarship hub (demo, aggregator)", "https://www.global-scholarship-hub.com/fully-funded-masters-2027", "web"),
    ("Oldtown University (demo, expired)", "https://www.oldtown.ac.uk/msc-data-analytics-scholarship", "university"),
]


def setup_demo(settings: Settings, pin: str | None = "1234") -> dict:
    home = settings.home
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data.update({"fetch_mode": "demo", "mail_source": "demo", "app": {"host": "127.0.0.1", "port": 8765,
                                                                       "dev_approvals": True}})
    cfg.write_text(json.dumps(data, indent=2))
    settings = load_settings(home)
    conn = connect(settings)
    vault.init_vault(settings)
    docs = settings.vault_dir / "documents"
    for name in ("transcript.pdf", "diploma.pdf", "passport.pdf", "ielts.pdf"):
        p = docs / name
        if not p.exists():
            p.write_bytes(b"%PDF-1.4\n% demo placeholder document\n")
    (docs / "cv_master.md").write_text("# Demo CV\n")
    if pin:
        vault.set_pin(settings, pin)
    for name, url, tier in DEMO_SOURCES:
        registry.add_source(conn, url, tier, name)
    conn.close()
    return {"home": str(home), "pin": pin, "sources": len(DEMO_SOURCES),
            "next": f"TRACKER_HOME={home} ./tracker.sh app --dev-approvals   then open the printed URL"}
