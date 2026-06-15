# Report Checker

Checks T&E draft reports against the team style guide using the Claude API,
and presents the results in a reviewer web UI.

It downloads one or more Google Docs from Drive, segments each into chunks at
the **paragraph** and **figure** level, tags every chunk with a
**document_type** (report / briefing / pr — read from the Cover tab) and a
**section** (cover / executive summary / main text / annex — from the document
tab titles), runs the applicable style rules, proposes rewrites for breaches,
and lets a reviewer accept/edit them straight back into the Google Doc.

Everything that varies — models, prompts, severities, page limits, which rules
apply where — lives in the `master_report_checker` Google Sheet, not in code.
All vocabulary is lowercased on read.

## What it produces

**Review** (per flagged paragraph/figure):
- a compact sidebar of every flagged extract, grouped by section, with
  red/amber counts — click to jump;
- the **extract** with the report's real formatting (bold/italic/underline/
  links) and the offending text highlighted;
- the **rules breached**, each independently **verified** (confirmed ✓ or
  refuted) with a one-line reason and, for the sentence-length check, the
  exact long sentence;
- a **suggested improvement** (clean rewritten text by default, "Show changes"
  for the word diff), and an **Edit mode** that lets you accept/reject each
  change, tweak formatting, and **commit the result to the live Google Doc**
  (one locked edit per paragraph).

**Document health** (mostly no AI):
- red / amber flag totals and approximate **pages vs the per-type page limit**;
- **broken links** (HTTP-checked; bot-walled hosts marked "unverified");
- **figure layout** coded checks — one figure per sub-section, full column
  width, footer ≤ 2 lines (via local OCR);
- **common words** (frequency with connecting words + T&E domain terms removed;
  AI marks the ones that look like a style issue);
- **what is my story?** — every numbered heading in order, with an AI verdict
  on whether they tell a convincing story.

## How the document must be structured

- One **document tab** per section: `Cover`, `Executive Summary`, numbered
  chapters, `Annex` (tabs, not headings — see the Google Docs sidebar).
- The `Cover` tab must state the document type somewhere in its text
  (`document_type: report`, or just `report`).
- Within a tab, Heading 1/2/3 and numbered lines (`1.2 …`) bound the
  sub-sections.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # set MASTER_SHEET_ID, ANTHROPIC_API_KEY
```

1. Drop `service_account.json` into the project root (gitignored).
2. In Google Cloud Console for that project, enable the **Google Docs**,
   **Google Drive** and **Google Sheets** APIs.
3. Share with the service account's `client_email`:
   - the `master_report_checker` spreadsheet and each report Doc — **Viewer**
     is enough for checking;
   - **Editor** on a report Doc is required to use the UI's commit-edit feature.

Figure text extraction uses the **Windows built-in OCR** engine (`winocr`), so
the pipeline runs on Windows.

## The config sheet

`master_report_checker` drives everything:

- **config tab** — `input_level`, `document_type` + `page_limit`, `section`,
  `check_severity` + its `flag_instruction`; a `tag` / `claude_model` / `role`
  / `max_token` block defining one AI step per row (flag check, rewrite,
  word-flag, story-flag, verification, plus the rewrite context blocks);
  `report_link` (one or several Google Doc links); `batching`, `cache`,
  `verify` (yes/no); `mode` + `max_pages`; `sentence_word_flag` +
  `sentence_word_limit`.
- **TE_style_rules tab** — one rule per row: `include_AI_check`
  (`yes` = AI-checked, `no` = off, `coded` = deterministic code check),
  the rule as `Rule: … Example: …`, its `level`, an optional `figure_type`
  (header / sub_header / footer / whole_image), and yes/no columns per
  document_type and per section.

## Run it

```powershell
.venv\Scripts\python.exe test_run.py        # checks + verification + rewrites
.venv\Scripts\python.exe analyse_doc.py     # document-health analyses
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8077   # reviewer UI
```

- `test_run.py` checks every paragraph/figure within the first `max_pages`
  pages of each linked report at the configured severity, runs the
  verification pass on each flag, and rewrites breached paragraphs. With
  `batching = yes` both loops go through the Message Batches API (50% token
  cost); `cache = yes` caches the system prompts.
- Outputs land per report in `data/runs/<doc_id>/` (`test_run.csv/json`,
  `analysis.json`), indexed in `data/runs/index.json`; the UI shows a report
  selector when more than one exists. `flag_history.csv` accumulates per-rule
  red/amber counts across reports over time.
- `export_report.py` builds `output/<title>/Open AI Report Check.html` — a
  single self-contained, read-only file (data + images + logo inlined) to send
  to anyone; no server or setup needed (commit-edit is disabled in exports).

`ingest.py` is the standalone phase-1 segmenter (writes `data/chunks.json`);
`extract_figure_text.py` writes `data/figure_text.csv` (figure header /
subheader / legend / footer text + line counts).

## Project structure

```
report_analyser_test/
├── app/
│   ├── auth.py          # service-account Google API clients (Docs read+write)
│   ├── docs_parser.py   # tabbed Google Doc → tagged chunks (+ formatting, links, headings)
│   ├── styleguide.py    # master_report_checker config + rules loader
│   ├── check_engine.py  # Claude calls: flag, verify, rewrite, word-flag, story-flag; batching/cache
│   ├── coded_checks.py  # deterministic checks (sentence length …)
│   ├── analysis.py      # broken links, word frequency, figure layout, story
│   ├── figure_parts.py  # Windows-OCR figure header/subheader/legend/footer
│   ├── doc_editor.py    # apply reviewed edits back to the Google Doc
│   ├── runs.py          # per-report output store + commit locks
│   ├── checks.py        # preflight + post-run sanity checks
│   ├── runlog.py        # per-run logging
│   └── main.py          # FastAPI app + reviewer UI endpoints
├── static/              # reviewer UI (index.html, app.js, styles.css)
├── test_run.py · analyse_doc.py · export_report.py · ingest.py
└── data/                # generated output (gitignored)
```
