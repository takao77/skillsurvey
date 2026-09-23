# Skill Maturity Survey

Managers get a personal link, rate every skill for each job level they lead (Importance 0–4,
Required Upon Entry Y/N, Proficiency N/A or 1–5), and submit. Answers autosave. A script then
builds the Quant / Qual mapping workbook in the Wave 1 layout for HR.

```
skill-survey/
├── app.py               Flask web app: survey page + autosave/submit API
├── survey_core.py       database tables, skill library loader, answer validation
├── setup_links.py       creates one unique link per manager → out/manager_links.csv
├── build_workbook.py    submitted answers → out/Skill_Mapping_Wave2.xlsx
├── templates/
│   ├── survey.html      the manager survey (HTML/CSS/JS, no external libraries or fonts)
│   └── message.html     error / info page
└── data/
    ├── job_levels.csv   family, level_order, level_title, grade, short_name
    ├── skills.csv       family, skill_name, skill_definition
    └── managers.csv     name, email, family
```

## 1. Run it on your laptop

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

set DEMO_MODE=1                                          # macOS/Linux: export DEMO_MODE=1
python setup_links.py                                    # prints out/manager_links.csv
flask --app app run --debug                              # open a link from the CSV
```

`DEMO_MODE=1` adds a "Fill remaining with sample answers" button for demos. Leave it off in production.

## 2. Load the real skill library and managers

Replace the three CSVs in `data/` (the samples are Account Management and Accounting).
The `family` value must match exactly across all three files. Export from the Wave 1 workbook
or from HCM, save as CSV (UTF-8), then run `python setup_links.py` again. It's safe to rerun:
existing managers keep their link, new rows get one.

## 3. Send the invitations

`out/manager_links.csv` has `name, first_name, email, family, link, status`.

- **Outlook mail merge** (Word → Mailings → Start Mail Merge → E-mail, select the CSV), or
- **Power Automate**: "List rows present in a table" (the CSV in Excel/SharePoint) →
  "Send an email (V2)" with the `link` column.

Reminders: `python setup_links.py --pending` exports only managers who haven't submitted.

## 4. Build the workbook for HR

```bash
python build_workbook.py                  # out/Skill_Mapping_Wave2.xlsx
python build_workbook.py --na-threshold 1.5
python build_workbook.py --include-raw    # individual answers: keep within HR analytics
```

Tabs: `HR Insights` (progression breaks, low rater agreement, required-upon-entry skills),
`Response Status`, then `<Family>-Quant` and `<Family>-Qual` for each job family.

| Measure | Meaning |
|---|---|
| N Raters | managers who submitted for that family |
| IMP | average importance, 0–4 |
| PROF | average proficiency, 1–5; N/A answers are excluded |
| RUE | share answering "required upon entry", 0.00–1.00 |
| Qual label | ROUND(PROF) → Basic / Intermediate / Proficient / Advanced / Expert; N/A when IMP < threshold or most raters chose N/A |

Only *submitted* answers are counted. Drafts are saved but ignored until the manager submits.

## 5. Deploy on Azure (App Service, Linux, Python 3.11+)

1. Create the App Service and deploy this folder (VS Code Azure extension, `az webapp up`, or GitHub Actions).
2. **Startup command:** `gunicorn --bind=0.0.0.0 --timeout 600 app:app`
3. **App settings (environment variables):**

   | Setting | Example | Purpose |
   |---|---|---|
   | `DATABASE_URL` | `mssql+pyodbc://@srv.database.windows.net/skills?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMsi` | Azure SQL with managed identity (default is a local SQLite file) |
   | `SURVEY_CLOSES` | `2026-10-10` | last day answers are accepted |
   | `SURVEY_WAVE` | `Wave 2` | keeps waves separate in the same database |
   | `SURVEY_BASE_URL` | `https://skills-survey.azurewebsites.net` | used by `setup_links.py` to build links |
   | `REQUIRE_SSO_MATCH` | `1` | with sign-in on, a link opens only for the manager it was sent to |

4. **Sign-in:** App Service → Authentication → add Microsoft (Entra ID) provider, "Require authentication".
   Then set `REQUIRE_SSO_MATCH=1` so forwarded links don't work for other people.
5. Tables are created automatically on first start. Run `setup_links.py` / `build_workbook.py`
   from your machine with the same `DATABASE_URL` (or as an App Service SSH / WebJob task).

SQLite is fine for a local demo; use Azure SQL in production, because App Service can run
several instances and its file system isn't a database.

## Security notes

- Links use 128-bit random tokens (`secrets.token_urlsafe(16)`), so they can't be guessed.
- The server keeps only known skills and levels, and valid values, from each save. Submit is
  rejected until every answer is filled in.
- Individual answers are stored per manager. Share the Quant/Qual tabs with HR; keep
  `--include-raw` output within the analytics team, as the survey promises managers.
- Get IT security / data privacy sign-off before production use.
