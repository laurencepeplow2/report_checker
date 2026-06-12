"""Document health analyses: broken links, overused words, story.

Links / word counts / story need no AI. One optional AI step classifies
which overused words are a style issue (model: config "Overused words").

Usage:
    python analyse_doc.py [doc_id]

Writes data/analysis.json for the UI.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from app.analysis import check_links, figure_layout, story, word_frequency
from app.check_engine import run_word_flagging
from app.checks import preflight
from app.docs_parser import DEFAULT_DOCUMENT_TYPES, parse_document
from app.runlog import setup_logging
from app.styleguide import StyleGuideConfig, load_config

log = logging.getLogger("report_checker.analyse")

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"

load_dotenv()


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else TEST_DOC_ID
    log_path = setup_logging("analyse_doc")
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("config not loaded (%s); using defaults", exc)
        config = StyleGuideConfig(document_types=DEFAULT_DOCUMENT_TYPES)
    allowed = config.document_types or DEFAULT_DOCUMENT_TYPES

    if not preflight(config=config, require_api_key=False, doc_id=doc_id):
        sys.exit(1)

    parsed = parse_document(doc_id, allowed_types=allowed)
    log.info("%r: %d links, %d headings, column width %.0fpt",
             parsed.title, len(parsed.links), len(parsed.headings),
             parsed.column_width_pt)

    log.info("Checking links (HTTP)...")
    links = check_links(parsed)
    log.info("  %d unique links, %d broken, %d unverified",
             links["unique_links"], links["broken_count"], links["unverified_count"])

    words = word_frequency(parsed)
    headings = story(parsed)
    layout = figure_layout(parsed)
    log.info("Figure layout: %d multi-figure subsections, %d figures below "
             "%d%% column width",
             len(layout["multi_figure_subsections"]), len(layout["narrow_figures"]), 90)
    pages_excl_annex = max(
        (c.approx_page for c in parsed.chunks if c.section != "annex"),
        default=0,
    )

    # AI step: which overused words are a style issue (vs subject matter)?
    word_model = config.model_for("overused words")
    if words and word_model and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            flagged = run_word_flagging(anthropic.Anthropic(), word_model, words, config)
            for w in words:
                w["flagged"] = w["word"] in flagged
                w["reason"] = flagged.get(w["word"], "")
            log.info("AI-flagged words (%s): %s", word_model,
                     ", ".join(sorted(flagged)) if flagged else "none")
        except Exception as exc:  # noqa: BLE001 — health page works without AI
            log.warning("word flagging failed (%s); words left unflagged", exc)
    else:
        log.warning("Skipping AI word flagging (no model or API key configured)")

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / "analysis.json"
    out.write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "approx_pages_excl_annex": pages_excl_annex,
                "links": links,
                "word_frequency": words,
                "story": headings,
                "figure_layout": layout,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log.info("-> %s", out)
    if words:
        top = ", ".join(f"{w['word']} ({w['count']})" for w in words[:8])
        log.info("Top words: %s", top)
    log.info("Full log: %s", log_path)


if __name__ == "__main__":
    main()
