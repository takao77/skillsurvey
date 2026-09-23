"""Shared pieces: database tables, skill library loading, and answer validation.

Works with SQLite for local testing and Azure SQL in production. Set DATABASE_URL, e.g.
  sqlite:///skill_survey.db
  mssql+pyodbc://@<server>.database.windows.net/<db>?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import (Column, DateTime, MetaData, String, Table, UnicodeText,
                        create_engine, insert, select, update)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SURVEY_DATA_DIR", BASE_DIR / "data"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'skill_survey.db'}")
WAVE = os.getenv("SURVEY_WAVE", "Wave 2")

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
meta = MetaData()

managers = Table(
    "survey_managers", meta,
    Column("token", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("email", String(320), nullable=False),
    Column("family", String(200), nullable=False),
    Column("wave", String(50), nullable=False),
    Column("created_at", DateTime, nullable=False),
)

responses = Table(
    "survey_responses", meta,
    Column("token", String(64), primary_key=True),
    Column("working_data", UnicodeText, nullable=False),   # autosaved draft (JSON)
    Column("submitted_data", UnicodeText, nullable=True),  # last submitted answers (JSON) - used for analysis
    Column("updated_at", DateTime, nullable=False),
    Column("submitted_at", DateTime, nullable=True),
    Column("confirmation", String(40), nullable=True),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as naive UTC


def iso(ts: datetime | None) -> str | None:
    return ts.replace(tzinfo=timezone.utc).isoformat() if ts else None


def init_db() -> None:
    meta.create_all(engine)


# ---------------------------------------------------------------- skill library
@lru_cache(maxsize=1)
def load_library() -> dict:
    """{family: {"levels": [{title, grade, short}], "skills": [{name, definition}]}}"""
    lib: dict = {}
    with open(DATA_DIR / "job_levels.csv", newline="", encoding="utf-8-sig") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: (r["family"], int(r["level_order"])))
    for r in rows:
        fam = lib.setdefault(r["family"].strip(), {"levels": [], "skills": []})
        fam["levels"].append({"title": r["level_title"].strip(), "grade": r["grade"].strip(),
                              "short": (r.get("short_name") or r["level_title"]).strip()})
    with open(DATA_DIR / "skills.csv", newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            fam = r["family"].strip()
            if fam not in lib:
                raise ValueError(f"skills.csv: family '{fam}' has no rows in job_levels.csv")
            lib[fam]["skills"].append({"name": r["skill_name"].strip(),
                                       "definition": r["skill_definition"].strip()})
    return lib


# ---------------------------------------------------------------- answers
# One answer per skill x level:  {"imp": 0-4, "prof": 0-5 (0 = N/A), "rue": 0/1}; None = not answered yet.
RANGES = {"imp": range(0, 5), "prof": range(0, 6), "rue": range(0, 2)}


def clean_answers(raw: dict, family: str) -> dict:
    """Keep only known skills/levels and valid values. Anything else is dropped."""
    fam = load_library()[family]
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for s in fam["skills"]:
        skill_raw = raw.get(s["name"]) if isinstance(raw.get(s["name"]), dict) else {}
        out[s["name"]] = {}
        for lv in fam["levels"]:
            cell = skill_raw.get(lv["title"]) if isinstance(skill_raw.get(lv["title"]), dict) else {}
            clean = {}
            for key, allowed in RANGES.items():
                v = cell.get(key)
                clean[key] = v if isinstance(v, int) and not isinstance(v, bool) and v in allowed else None
            out[s["name"]][lv["title"]] = clean
    return out


def missing_count(answers: dict, family: str) -> int:
    fam = load_library()[family]
    return sum(1 for s in fam["skills"] for lv in fam["levels"]
               for v in answers[s["name"]][lv["title"]].values() if v is None)


# ---------------------------------------------------------------- data access
def get_manager(token: str):
    with engine.connect() as c:
        return c.execute(select(managers).where(managers.c.token == token)).mappings().first()


def get_response(token: str):
    with engine.connect() as c:
        return c.execute(select(responses).where(responses.c.token == token)).mappings().first()


def save_response(token: str, **values) -> None:
    values = {k: (json.dumps(v) if k.endswith("_data") and v is not None else v) for k, v in values.items()}
    with engine.begin() as c:
        exists = c.execute(select(responses.c.token).where(responses.c.token == token)).first()
        if exists:
            c.execute(update(responses).where(responses.c.token == token).values(**values))
        else:
            c.execute(insert(responses).values(token=token, **values))
