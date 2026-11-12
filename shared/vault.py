"""Private profile vault. Only tracker code reads it; agents receive summaries.

Files (TRACKER_HOME/vault, mode 700, files 600):
  profile.json   academic profile, contacts, referees, documents
  pin.json       scrypt hash of the approval PIN
  gmail_token.json  OAuth token (written by `tracker mail auth`)

Encryption at rest is still open (see ARCHITECTURE.md). Use full-disk encryption meanwhile.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from .config import Settings

SAMPLE_PROFILE = {
    "full_name": "Your Name",
    "email": "you@example.com",
    "phone": "+251 900 000 000",
    "date_of_birth": "1999-01-01",
    "nationality": "Ethiopia",
    "country_of_residence": "Ethiopia",
    "degree": {"level": "bachelor", "field": "Computer Science", "university": "Your University",
               "graduation_year": 2022, "gpa": 3.6, "gpa_scale": 4.0},
    "english": {"test": "IELTS", "score": 7.0, "date": "2026-06-01"},
    "work_experience_years": 2,
    "target_fields": ["Artificial Intelligence", "Machine Learning", "Data Science", "Computer Science"],
    "interests": "machine learning for healthcare, low-resource NLP",
    "career_goal": "build AI systems that improve public services in East Africa",
    "experience": [
        {"title": "Software Engineer", "org": "Example Tech PLC", "from": "2022-09", "to": "present",
         "summary": "Built data pipelines and ML models for demand forecasting."}
    ],
    "skills": ["Python", "PyTorch", "SQL", "Docker"],
    "referees": [
        {"name": "Dr. Referee One", "email": "referee1@example.edu", "relation": "thesis supervisor"},
        {"name": "Referee Two", "email": "referee2@example.com", "relation": "line manager"},
    ],
    "documents": {
        "transcript": "documents/transcript.pdf",
        "diploma": "documents/diploma.pdf",
        "passport": "documents/passport.pdf",
        "english_test": "documents/ielts.pdf",
        "cv_master": "documents/cv_master.md",
    },
}


class VaultError(Exception):
    pass


def init_vault(settings: Settings) -> Path:
    d = settings.vault_dir
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = d / "profile.json"
    if not p.exists():
        p.write_text(json.dumps(SAMPLE_PROFILE, indent=2))
    os.chmod(p, 0o600)
    (d / "documents").mkdir(exist_ok=True)
    os.chmod(d / "documents", 0o700)
    return d


def check_permissions(settings: Settings) -> list[str]:
    problems = []
    d = settings.vault_dir
    if not d.exists():
        return ["vault does not exist (run `tracker init`)"]
    if stat.S_IMODE(d.stat().st_mode) & 0o077:
        problems.append(f"{d} is readable by others (chmod 700)")
    for f in d.glob("*.json"):
        if stat.S_IMODE(f.stat().st_mode) & 0o077:
            problems.append(f"{f} is readable by others (chmod 600)")
    return problems


def load_profile(settings: Settings) -> dict:
    p = settings.vault_dir / "profile.json"
    if not p.exists():
        raise VaultError("no profile; run `tracker init` and edit vault/profile.json")
    return json.loads(p.read_text())


def profile_summary(profile: dict) -> dict:
    """What an agent may see: no contacts, no documents, no referees' details."""
    deg = profile.get("degree", {})
    return {
        "degree": f"{deg.get('level')} in {deg.get('field')}",
        "gpa": f"{deg.get('gpa')}/{deg.get('gpa_scale')}",
        "english": f"{profile.get('english', {}).get('test')} {profile.get('english', {}).get('score')}",
        "target_fields": profile.get("target_fields", []),
        "work_experience_years": profile.get("work_experience_years"),
        "referees": len(profile.get("referees", [])),
    }


def document_path(settings: Settings, rel: str) -> Path:
    p = (settings.vault_dir / rel).resolve()
    if settings.vault_dir.resolve() not in p.parents:
        raise VaultError("document path escapes the vault")
    return p


# ---- approval PIN -------------------------------------------------------

def _scrypt(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def set_pin(settings: Settings, pin: str) -> None:
    if not (pin.isdigit() and 4 <= len(pin) <= 12):
        raise VaultError("PIN must be 4-12 digits")
    salt = secrets.token_bytes(16)
    init_vault(settings)
    p = settings.vault_dir / "pin.json"
    p.write_text(json.dumps({"salt": salt.hex(), "hash": _scrypt(pin, salt).hex()}))
    os.chmod(p, 0o600)


def pin_is_set(settings: Settings) -> bool:
    return (settings.vault_dir / "pin.json").exists()


def check_pin(settings: Settings, pin: str) -> bool:
    p = settings.vault_dir / "pin.json"
    if not p.exists() or not pin:
        return False
    data = json.loads(p.read_text())
    return hmac.compare_digest(_scrypt(pin, bytes.fromhex(data["salt"])).hex(), data["hash"])
