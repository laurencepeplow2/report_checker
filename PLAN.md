# Report Checker — Implementation Plan

A web app that pulls a Google Doc / Slides deck from Drive, segments it into
chunks at three input levels (**paragraph / figure / sub-section incl.
figures**), tags each chunk with **document_type** (report / briefing / pr)
and **section** (cover / executive summary / main / annex — vocab from the
config tab), runs the applicable checks from the `master_report_checker`
Google Sheet style guide through the Claude API, and presents results in a
reviewer UI that cycles chunk-by-chunk with colour-coded issue highlighting.

- **Stack:** Python, FastAPI backend + vanilla HTML/JS frontend
- **Auth:** Google service account (Docs / Drive / Sheets)
- **Audience:** must be easily sharable and usable by non-technical people —
  everything happens in the browser once running; no CLI knowledge needed.
  Later: one-click hosted deployment (e.g. Cloud Run) so nobody installs
  anything.
- **Progress tracked on GitHub.**
- All text comparisons (document_type, section, config values) are
  **lowercased** — the source sheet and docs are hand-edited.

## Key design decisions (locked in)

1. **Verbatim quotes, not character offsets, for highlighting.** Each issue
   must include an exact quote of the offending text; the backend
   substring-validates it against the chunk before highlighting. A
   non-matching quote = hallucinated issue → downgraded, never
   mis-highlighted.
2. **Hybrid deterministic + LLM checks.** A `check_type` column
   (`regex` | `llm`) in the checks tab: mechanical rules (banned words,
   number/date formats) run in Python for free with perfect precision;
   Claude handles judgement calls only.
3. **Verification pass on red flags.** Before a red flag is shown, a second
   cheap Claude call confirms "is this quote genuinely a violation of this
   rule?" — only doubles cost on the flagged minority, cuts false positives.

## Document structure (watch-outs)

- Reports use **Google Docs document tabs** (Cover / Executive Summary /
  numbered chapters / Annex). The Docs API returns tab content only with
  `includeTabsContent=true`; content lives in `tabs[].documentTab`, and tabs
  can nest (`childTabs`).
- The **Cover tab is discarded** except for the `document_type` it declares.
- `section` is derived from tab titles (lowercased): "executive summary" →
  executive summary, "annex" → annex, everything else (numbered chapters,
  recommendations) → main.
- Within a tab, Heading 1/2/3 paragraphs bound the sub-section chunks.

## Phases

1. **Ingestion (current)** — `ingest.py`: doc → `data/chunks.json` + figure
   images, tagged with input_level / document_type / section.
2. **Style guide loader** — config tab (claude_model, check_severity,
   vocab lists) + checks tab (~100 rows: check_id, check_text, severity,
   check_type, one input_level, multi document_type, multi section).
   Validated on load; malformed rows surfaced, not fatal.
3. **Check engine** — per chunk: regex checks in Python; remaining checks
   batched (~15–25 per Claude call) with structured outputs
   `{check_id, verdict: pass|amber|red, quote, suggestion}`; style-guide
   block as cached prompt prefix; verification pass on reds; SQLite cache
   keyed on (chunk_hash, check_set_hash, model) so re-runs after edits only
   re-process changed chunks.
4. **Reviewer UI (FastAPI + JS)** — picker page (doc, severity, pre-run cost
   estimate) → review page cycling one chunk at a time: input_level
   dropdown, original text with red/amber highlights colour-matched to issue
   cards, suggestion panel, accept/reject per issue.
5. **Verification loop** — uvicorn + Playwright screenshots to iterate on
   the UI against the real test doc.

## Later / nice-to-have

- Per-check precision dashboard from accept/reject logs.
- Results written back to a sheet tab for team audit.
- Document-level checks (acronym defined at first use, cross-section
  consistency) as a fourth input level.
- Hosted deployment for the team.
