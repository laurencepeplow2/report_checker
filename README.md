# Report Checker

Checks T&E draft reports against the team style guide using the Claude API.

It downloads a Google Doc from Drive, segments it into chunks at three
**input levels** (paragraph / figure / sub-section), tags each chunk with a
**document_type** (report / briefing / pr — read from the Cover tab) and a
**section** (cover / executive summary / main / annex — derived from the
document tab titles), then runs the applicable checks from the
`master_report_checker` Google Sheet and shows results in a reviewer web UI.
Both vocabularies live in the sheet's `config` tab; everything is lowercased
on read.

Built for non-technical users: once set up, everything runs from the browser.

## Status

**Phase 1 — ingestion** (current): download + segment a tabbed Google Doc.

Planned: style-guide check engine (verbatim-quote highlighting, hybrid
regex/LLM checks, verification pass on red flags), FastAPI + JS reviewer UI.

## How the document must be structured

- One **document tab** per section: `Cover`, `Executive Summary`,
  numbered chapters, `Annex` (tabs, not headings — see the sidebar in
  Google Docs).
- The `Cover` tab must state the document type somewhere in its text
  (e.g. `document_type: report` or just `report`). Matching is
  case-insensitive; everything is lowercased on read.
- Within a tab, headings (Heading 1/2/3…) define the sub-section chunks.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

1. Drop `service_account.json` into the project root (gitignored).
2. In Google Cloud Console for that service account's project, enable the
   **Google Docs API**, **Google Drive API** and **Google Sheets API**.
3. Share with the service account's `client_email` (Viewer is enough):
   - the report Google Doc
   - the `master_report_checker` spreadsheet

## Run ingestion

```powershell
.venv\Scripts\python.exe ingest.py            # uses the built-in test doc id
.venv\Scripts\python.exe ingest.py <doc_id>   # any other doc
```

Outputs `data/chunks.json` and figure images in `data/images/`, plus a
console summary of chunks per tab / input level.

## Run the test-mode checks

```powershell
.venv\Scripts\python.exe test_run.py
```

Samples one figure + one paragraph each from the executive summary, main
text and recommendations, runs every applicable `TE_style_rules` rule at
severity `high`, and writes `data/test_run.csv` (full prompts + r/a/g
flags) and `data/test_run.json` (for the UI). Needs `ANTHROPIC_API_KEY`
in `.env`.

## Run the reviewer UI

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8077
```

Open http://127.0.0.1:8077 — cycle chunk by chunk (arrow keys work),
filter by input level, see red/amber/green cards per rule and the figure
images inline.

## Project structure

```
report_analyser_test/
├── app/
│   ├── auth.py          # service-account Google API clients
│   ├── docs_parser.py   # tabbed Google Doc → tagged chunks
│   └── styleguide.py    # master_report_checker config loader
├── ingest.py            # phase 1 CLI
├── scripts/             # one-off exploration/diagnostic scripts
└── data/                # generated output (gitignored)
```
