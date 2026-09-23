"""Manager skill maturity survey - Flask app.

Each manager opens /s/<token> (their unique link), rates every skill x job level,
answers autosave to the database, and Submit stores the final answers.

Run locally:   flask --app app run --debug
Azure:         gunicorn --bind=0.0.0.0 --timeout 600 app:app
"""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
from datetime import date

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from sqlalchemy import select

from survey_core import (WAVE, clean_answers, engine, get_manager, get_response, init_db, iso,
                         load_library, managers, missing_count, responses, save_response, utcnow)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # a full survey is ~20 KB

SURVEY_CLOSES = date.fromisoformat(os.getenv("SURVEY_CLOSES", "2026-10-10"))  # last day to answer
# With Entra ID sign-in (App Service "Easy Auth") turned on, set REQUIRE_SSO_MATCH=1 so a link
# only opens for the manager it was sent to - a forwarded link won't work for anyone else.
REQUIRE_SSO_MATCH = os.getenv("REQUIRE_SSO_MATCH", "0") == "1"
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"  # shows a "fill sample answers" button for demos
# Who can open /admin in production: comma-separated emails, checked against Entra ID sign-in.
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
MIN_RATERS = int(os.getenv("MIN_RATERS", "3"))  # results are treated as reliable from this many submissions

init_db()


def is_closed() -> bool:
    return date.today() > SURVEY_CLOSES


def load_manager_or_404(token: str):
    m = get_manager(token)
    if not m:
        abort(404)
    if REQUIRE_SSO_MATCH:
        signed_in = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "").strip().lower()
        if signed_in != m["email"].strip().lower():
            abort(403)
    return m


@app.errorhandler(404)
def not_found(_):
    return render_template("message.html", title="This link doesn't work",
                           body="Check that you copied the whole link from your invitation email, "
                                "or contact the HR analytics team for a new one."), 404


@app.errorhandler(403)
def forbidden(_):
    return render_template("message.html", title="This link belongs to another manager",
                           body="Survey links are personal. Open the link from your own invitation "
                                "email, or contact the HR analytics team."), 403


@app.get("/")
def home():
    return render_template("message.html", title="Skill Maturity Survey",
                           body="Open the personal link from your invitation email to start."), 200


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/s/<token>")
def survey(token: str):
    m = load_manager_or_404(token)
    fam = load_library()[m["family"]]
    r = get_response(token)
    first = m["name"].split()[0]
    config = {
        "api": f"/api/{token}",
        "manager": {"name": m["name"], "first_name": first, "email": m["email"],
                    "initials": "".join(p[0] for p in m["name"].split()[:2]).upper()},
        "family": m["family"],
        "wave": m["wave"] or WAVE,
        "closes_label": SURVEY_CLOSES.strftime("%b %d").replace(" 0", " "),
        "closed": is_closed(),
        "levels": fam["levels"],
        "skills": fam["skills"],
        "data": json.loads(r["working_data"]) if r else {},
        "submitted_at": iso(r["submitted_at"]) if r else None,
        "confirmation": r["confirmation"] if r else None,
        "demo": DEMO_MODE,
    }
    return render_template("survey.html", config=config)


def _read_answers(m):
    body = request.get_json(silent=True) or {}
    return clean_answers(body.get("data"), m["family"])


@app.post("/api/<token>/draft")
def save_draft(token: str):
    m = load_manager_or_404(token)
    if is_closed():
        return jsonify(error="The survey has closed."), 409
    answers = _read_answers(m)
    now = utcnow()
    save_response(token, working_data=answers, updated_at=now)
    return jsonify(ok=True, updated_at=iso(now))


@app.post("/api/<token>/submit")
def submit(token: str):
    m = load_manager_or_404(token)
    if is_closed():
        return jsonify(error="The survey has closed."), 409
    answers = _read_answers(m)
    left = missing_count(answers, m["family"])
    if left:
        return jsonify(error=f"{left} answers are still missing."), 422
    now = utcnow()
    prev = get_response(token)
    confirmation = (prev and prev["confirmation"]) or \
        f"{''.join(w[0] for w in m['family'].split())[:3].upper()}-{secrets.token_hex(3).upper()}"
    save_response(token, working_data=answers, submitted_data=answers, updated_at=now,
                  submitted_at=now, confirmation=confirmation)
    return jsonify(ok=True, confirmation=confirmation, submitted_at=iso(now))


# =====================================================================  admin (HR only)
def is_admin() -> bool:
    """Production: the signed-in Entra ID user must be in ADMIN_EMAILS.
    Local laptop (http://127.0.0.1 or localhost, not behind Azure's proxy): always allowed."""
    signed_in = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "").strip().lower()
    if ADMIN_EMAILS:
        return signed_in in ADMIN_EMAILS
    host = request.host.split(":")[0]
    behind_proxy = "X-Forwarded-For" in request.headers or "X-ARR-SSL" in request.headers
    return host in ("127.0.0.1", "localhost") and not behind_proxy


def require_admin():
    if not is_admin():
        return render_template("message.html", title="Admins only",
                               body="This page is for the HR analytics team. Ask the survey owner "
                                    "to add your email to ADMIN_EMAILS."), 403
    return None


def base_url() -> str:
    return os.getenv("SURVEY_BASE_URL", request.host_url).rstrip("/")


def manager_status() -> list[dict]:
    """One row per invited manager with progress and status."""
    lib = load_library()
    q = (select(managers.c.token, managers.c.name, managers.c.email, managers.c.family,
                responses.c.working_data, responses.c.updated_at, responses.c.submitted_at)
         .select_from(managers.outerjoin(responses, managers.c.token == responses.c.token))
         .where(managers.c.wave == WAVE).order_by(managers.c.family, managers.c.name))
    rows = []
    with engine.connect() as c:
        for r in c.execute(q).mappings():
            fam = lib.get(r["family"])
            if not fam:
                continue
            total = len(fam["skills"]) * len(fam["levels"]) * 3
            answers = clean_answers(json.loads(r["working_data"]), r["family"]) if r["working_data"] else None
            pct = 1 - missing_count(answers, r["family"]) / total if answers else 0.0
            status = ("Submitted" if r["submitted_at"] else "In progress" if pct > 0 else "Not started")
            rows.append({"name": r["name"], "email": r["email"], "family": r["family"],
                         "status": status, "pct": round(pct, 3),
                         "last": iso(r["updated_at"]) if r["updated_at"] else None,
                         "link": f"{base_url()}/s/{r['token']}"})
    return rows


@app.get("/admin")
def admin():
    denied = require_admin()
    if denied:
        return denied
    from build_workbook import aggregate, family_flags, load_submissions, qual_label

    threshold = request.args.get("thr", default=1.5, type=float)
    subs = load_submissions()
    fams = {}
    for family, fam in load_library().items():
        A = aggregate(family, subs)
        fams[family] = {
            "levels": fam["levels"],
            "raters": sum(1 for s in subs if s["family"] == family),
            "rows": [{"skill": sk["name"], "cells": [
                {**{k: A[(sk["name"], lv["title"])][k] for k in ("n", "imp", "prof", "rue")},
                 "label": qual_label(A[(sk["name"], lv["title"])], threshold)}
                for lv in fam["levels"]]} for sk in fam["skills"]],
            "flags": [{"skill": f[1], "flag": f[2], "detail": f[3]} for f in family_flags(family, fam, A)],
        }
    data = {"wave": WAVE, "closes": SURVEY_CLOSES.isoformat(),
            "closes_label": SURVEY_CLOSES.strftime("%b %d").replace(" 0", " "),
            "days_left": max(0, (SURVEY_CLOSES - date.today()).days), "closed": is_closed(),
            "min_raters": MIN_RATERS, "threshold": threshold,
            "managers": manager_status(), "families": fams}
    return render_template("admin.html", data=data)


@app.get("/admin/pending.csv")
def admin_pending_csv():
    denied = require_admin()
    if denied:
        return denied
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["name", "first_name", "email", "family", "link", "status", "progress"])
    for m in manager_status():
        if m["status"] != "Submitted":
            w.writerow([m["name"], m["name"].split()[0], m["email"], m["family"], m["link"],
                        m["status"], f"{m['pct']:.0%}"])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pending_managers.csv"})


@app.get("/admin/workbook.xlsx")
def admin_workbook():
    denied = require_admin()
    if denied:
        return denied
    from build_workbook import build

    threshold = request.args.get("thr", default=1.5, type=float)
    buf = io.BytesIO()
    build(threshold=threshold, include_raw=False).save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"Skill_Mapping_{WAVE.replace(' ', '')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app.run(debug=True)
