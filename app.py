"""Manager skill maturity survey - Flask app.

Each manager opens /s/<token> (their unique link), rates every skill x job level,
answers autosave to the database, and Submit stores the final answers.

Run locally:   flask --app app run --debug
Azure:         gunicorn --bind=0.0.0.0 --timeout 600 app:app
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import date

from flask import Flask, abort, jsonify, render_template, request

from survey_core import (WAVE, clean_answers, get_manager, get_response, init_db, iso,
                         load_library, missing_count, save_response, utcnow)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # a full survey is ~20 KB

SURVEY_CLOSES = date.fromisoformat(os.getenv("SURVEY_CLOSES", "2026-10-10"))  # last day to answer
# With Entra ID sign-in (App Service "Easy Auth") turned on, set REQUIRE_SSO_MATCH=1 so a link
# only opens for the manager it was sent to - a forwarded link won't work for anyone else.
REQUIRE_SSO_MATCH = os.getenv("REQUIRE_SSO_MATCH", "0") == "1"
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"  # shows a "fill sample answers" button for demos

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


if __name__ == "__main__":
    app.run(debug=True)
