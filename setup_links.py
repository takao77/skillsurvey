"""Create a unique survey link for every manager and export the list for the invitation email.

    python setup_links.py                      # reads data/managers.csv, writes out/manager_links.csv
    python setup_links.py --pending            # only managers who haven't submitted (for reminders)

managers.csv columns: name, email, family  (family must match job_levels.csv / skills.csv)
Running it again is safe: existing managers keep their link; new rows get a new one.
Send the CSV with an Outlook mail merge or a Power Automate flow ("Send an email (V2)" per row).
"""
from __future__ import annotations

import argparse
import csv
import os
import secrets
from pathlib import Path

from sqlalchemy import insert, select

from survey_core import (BASE_DIR, DATA_DIR, WAVE, engine, init_db, load_library, managers,
                         responses, utcnow)

BASE_URL = os.getenv("SURVEY_BASE_URL", "http://127.0.0.1:5000").rstrip("/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--managers", default=str(DATA_DIR / "managers.csv"))
    ap.add_argument("--out", default=str(BASE_DIR / "out" / "manager_links.csv"))
    ap.add_argument("--pending", action="store_true", help="export only managers who have not submitted")
    args = ap.parse_args()

    init_db()
    lib = load_library()
    created = 0
    with open(args.managers, newline="", encoding="utf-8-sig") as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]

    with engine.begin() as c:
        for r in rows:
            if r["family"] not in lib:
                raise SystemExit(f"Unknown job family '{r['family']}' for {r['email']} - "
                                 f"add it to job_levels.csv and skills.csv first.")
            found = c.execute(select(managers.c.token).where(
                (managers.c.email == r["email"].lower()) & (managers.c.family == r["family"])
                & (managers.c.wave == WAVE))).first()
            if not found:
                c.execute(insert(managers).values(token=secrets.token_urlsafe(16), name=r["name"],
                                                  email=r["email"].lower(), family=r["family"],
                                                  wave=WAVE, created_at=utcnow()))
                created += 1

        q = (select(managers.c.name, managers.c.email, managers.c.family, managers.c.token,
                    responses.c.submitted_at)
             .select_from(managers.outerjoin(responses, managers.c.token == responses.c.token))
             .where(managers.c.wave == WAVE).order_by(managers.c.family, managers.c.name))
        people = c.execute(q).mappings().all()

    if args.pending:
        people = [p for p in people if p["submitted_at"] is None]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "first_name", "email", "family", "link", "status"])
        for p in people:
            w.writerow([p["name"], p["name"].split()[0], p["email"], p["family"],
                        f"{BASE_URL}/s/{p['token']}", "Submitted" if p["submitted_at"] else "Pending"])
    print(f"{created} new link(s) created · {len(people)} row(s) written to {out}")


if __name__ == "__main__":
    main()
