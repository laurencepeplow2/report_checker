"""Top-20 pass/fail checks for the latest run, written to the
``automated_checks`` tab of the master sheet.

The point is a single at-a-glance view: after running a (possibly new-shaped)
document through the tool, did every stage actually work - was the document
parsed, the type and sections recognised, did the AI produce valid flags, did
verification / rewrites / the health analyses run, and was the cost sane?

Each check is PASS / WARN / FAIL. The checks read the run outputs on disk
(``data/runs/<doc_id>/test_run.json`` + ``analysis.json``) and the cost log,
so calling this from either test_run.py or analyse_doc.py reflects whatever
has been produced so far. Writing needs Editor access on the master sheet;
if that's missing (or the tab is absent) we log a warning and carry on.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from app.auth import sheets_service
from app.check_engine import usd_to_eur
from app.styleguide import find_sheet_id

log = logging.getLogger("report_checker.automated_checks")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "data" / "runs"
COST_LOG = PROJECT_ROOT / "run_cost_log.csv"
TAB = "automated_checks"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_SYMBOL = {PASS: "✓", WARN: "!", FAIL: "✗"}  # ✓ / ! / ✗


def _verdict(ok: bool, fail_level: str = FAIL) -> str:
    return PASS if ok else fail_level


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/partial file is just "stage not run"
        return None


def _last_cost_row(doc_id: str) -> dict | None:
    if not COST_LOG.exists():
        return None
    try:
        with COST_LOG.open(encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("doc_id") == doc_id]
        return rows[-1] if rows else None
    except Exception:  # noqa: BLE001
        return None


def build_checks(doc_id: str, config=None) -> list[tuple[str, str, str]]:
    """Return a list of (check name, PASS/WARN/FAIL, detail) for the latest
    run of one document. Ordered most-fundamental first."""
    out: list[tuple[str, str, str]] = []
    run_dir = RUNS_DIR / doc_id
    tr = _load_json(run_dir / "test_run.json")
    an = _load_json(run_dir / "analysis.json")
    cost = _last_cost_row(doc_id)

    # ---- parse / structure -------------------------------------------------
    chunks = (tr or {}).get("chunks", [])
    paras = [c for c in chunks if c.get("input_level") == "paragraph"]
    figs = [c for c in chunks if c.get("input_level") == "figure"]
    results = [r for c in chunks for r in c.get("results", [])]
    sections = sorted({c.get("section", "") for c in chunks if c.get("section")})

    out.append(("Document parsed", _verdict(bool(chunks)),
                f"{len(chunks)} chunks" if chunks else
                "no chunks - check run not found or document discarded"))
    doc_type = (tr or {}).get("document_type", "")
    out.append(("Document type recognised",
                _verdict(bool(doc_type) and doc_type != "unknown"),
                doc_type or "unknown"))
    out.append(("Sections detected",
                PASS if len(sections) >= 2 else (WARN if sections else FAIL),
                ", ".join(sections) or "none"))
    out.append(("Paragraphs found", _verdict(bool(paras)), f"{len(paras)} paragraphs"))
    out.append(("Figures detected", PASS if figs else WARN,
                f"{len(figs)} figures" if figs else "no figures found"))

    # ---- AI check run ------------------------------------------------------
    out.append(("AI checks executed", _verdict(bool(results)),
                f"{len(results)} (chunk x rule) results" if results
                else "no AI results"))
    invalid = [r for r in results if r.get("flag") not in ("r", "a", "g")]
    out.append(("All AI responses valid", _verdict(not invalid),
                "all r/a/g" if not invalid else f"{len(invalid)} invalid/blank flags"))
    live = [r for r in results
            if r.get("flag") in ("r", "a") and r.get("verdict") != "refuted"]
    greens = sum(1 for r in results if r.get("flag") == "g")
    out.append(("Flags look sane",
                WARN if (results and (not live or greens == 0)) else PASS,
                f"{len(live)} live breaches, {greens} green"
                if results else "no results"))

    verify_on = getattr(config, "verify", True)
    verdicts = [r for r in results if r.get("verdict") in ("confirmed", "refuted")]
    out.append(("Verification pass ran",
                PASS if (verdicts or not verify_on) else WARN,
                f"{len(verdicts)} flags verified" if verify_on
                else "verification disabled in config"))

    breached_paras = {c["chunk_id"] for c in chunks
                      if c.get("input_level") != "figure"
                      and any(r.get("flag") in ("r", "a")
                              and r.get("verdict") != "refuted"
                              for r in c.get("results", []))}
    with_sugg = {c["chunk_id"] for c in chunks
                 if c.get("chunk_id") in breached_paras and c.get("suggestion")}
    out.append(("Rewrites generated",
                PASS if (not breached_paras or with_sugg) else WARN,
                f"{len(with_sugg)}/{len(breached_paras)} breached paragraphs"
                if breached_paras else "no breached paragraphs to rewrite"))

    # ---- cost --------------------------------------------------------------
    out.append(("Run cost logged", _verdict(cost is not None),
                f"${cost['total_cost']}" if cost else "no cost-log row"))
    cap = getattr(config, "max_report_cost_eur", 0) or 0
    if cost and cap:
        eur = usd_to_eur(float(cost.get("total_cost", 0) or 0))
        out.append(("Cost within cap", _verdict(eur <= cap),
                    f"EUR {eur:.2f} of EUR {cap} cap"))
    else:
        out.append(("Cost within cap", PASS,
                    "no cap set" if not cap else "cost not logged yet"))

    # ---- health analyses (analysis.json) -----------------------------------
    out.append(("Health analysis present", _verdict(an is not None),
                "analysis.json found" if an else "analyse_doc not run yet"))
    links = (an or {}).get("links", {})
    out.append(("Links checked",
                PASS if links.get("unique_links") else WARN,
                f"{links.get('unique_links', 0)} unique, "
                f"{links.get('broken_count', 0)} broken"
                if links else "no link data"))
    words = (an or {}).get("word_frequency", [])
    out.append(("Common words computed", _verdict(bool(words)),
                f"{len(words)} words" if words else "none"))
    dist = (an or {}).get("sentence_lengths", [])
    dist_total = sum(d.get("count", 0) for d in dist)
    out.append(("Sentence distribution computed", _verdict(dist_total > 0),
                f"{dist_total} sentences" if dist_total else "none"))
    story = (an or {}).get("story", [])
    out.append(("Story arc built", _verdict(bool(story)),
                f"{len(story)} headings" if story else "none"))
    story_flag = (an or {}).get("story_flag", {})
    out.append(("Story verdict (AI) produced",
                PASS if story_flag.get("flag") else WARN,
                f"flag={story_flag.get('flag')}" if story_flag.get("flag")
                else "no AI story verdict"))
    msg_flagged = sum(1 for h in story if h.get("message_flag"))
    out.append(("Per-title message flags produced",
                PASS if msg_flagged else WARN,
                f"{msg_flagged}/{len(story)} headings flagged" if story else "none"))
    pages = (an or {}).get("approx_pages_excl_annex", 0)
    out.append(("Page count computed", _verdict(bool(pages)),
                f"~{pages} pages (excl. annex)" if pages else "not computed"))

    return out[:20]


def write_automated_checks(doc_id: str, config=None,
                           sheet_id: str | None = None) -> bool:
    """Write the top-20 checks for ``doc_id`` to the ``automated_checks`` tab.
    Returns True on success; logs a warning and returns False if the tab is
    missing or the service account lacks Editor access (non-fatal)."""
    checks = build_checks(doc_id, config)
    n_fail = sum(1 for _, lvl, _ in checks if lvl == FAIL)
    n_warn = sum(1 for _, lvl, _ in checks if lvl == WARN)
    overall = FAIL if n_fail else (WARN if n_warn else PASS)
    tr = _load_json(RUNS_DIR / doc_id / "test_run.json") or {}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = [
        ["Automated checks - latest run", "", f"updated {ts}"],
        ["Document", tr.get("title", ""), doc_id],
        ["OVERALL", f"{_SYMBOL[overall]} {overall}",
         f"{len(checks) - n_fail - n_warn} pass, {n_warn} warn, {n_fail} fail"],
        ["", "", ""],
        ["Check", "Result", "Detail"],
    ]
    body = [[name, f"{_SYMBOL[lvl]} {lvl}", detail] for name, lvl, detail in checks]
    values = header + body

    try:
        sid = sheet_id or find_sheet_id()
        svc = sheets_service().spreadsheets().values()
        svc.clear(spreadsheetId=sid, range=f"'{TAB}'!A1:C100").execute()
        svc.update(spreadsheetId=sid, range=f"'{TAB}'!A1",
                   valueInputOption="RAW", body={"values": values}).execute()
        log.info("Automated checks -> '%s' tab: %s (%d pass, %d warn, %d fail)",
                 TAB, overall, len(checks) - n_fail - n_warn, n_warn, n_fail)
        return True
    except Exception as exc:  # noqa: BLE001 - never break a run over this
        msg = str(exc)
        if "Unable to parse range" in msg or "not found" in msg.lower():
            log.warning("Could not write automated checks: is there an '%s' tab "
                        "on the sheet? (%s)", TAB, msg[:120])
        else:
            log.warning("Could not write automated checks (Editor access on the "
                        "master sheet required): %s", msg[:160])
        return False
