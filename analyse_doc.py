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

from app.analysis import (
    check_links, figure_layout, formatting_checks, sentence_length_distribution,
    story, word_frequency,
)
from app.check_engine import run_message_flag, run_story_flag, run_word_flagging
from app.checks import preflight
from app.docs_parser import DEFAULT_DOCUMENT_TYPES, parse_document
from app.runlog import setup_logging
from app.runs import run_dir, update_index
from app.styleguide import StyleGuideConfig, load_config

log = logging.getLogger("report_checker.analyse")

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"

load_dotenv()


def main() -> None:
    log_path = setup_logging("analyse_doc")
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("config not loaded (%s); using defaults", exc)
        config = StyleGuideConfig(document_types=DEFAULT_DOCUMENT_TYPES)
    doc_ids = ([sys.argv[1]] if len(sys.argv) > 1
               else config.report_doc_ids or [TEST_DOC_ID])

    ok = all(
        preflight(config=config if i == 0 else None,
                  require_api_key=False, doc_id=d)
        for i, d in enumerate(doc_ids)
    )
    if not ok:
        sys.exit(1)

    for doc_id in doc_ids:
        analyse_for_doc(config, doc_id)
    log.info("Full log: %s", log_path)


def analyse_for_doc(config: StyleGuideConfig, doc_id: str) -> None:
    allowed = config.document_types or DEFAULT_DOCUMENT_TYPES
    parsed = parse_document(doc_id, allowed_types=allowed,
                            image_dir=DATA_DIR / "images")
    log.info("%r: %d links, %d headings, column width %.0fpt",
             parsed.title, len(parsed.links), len(parsed.headings),
             parsed.column_width_pt)

    log.info("Checking links (HTTP)...")
    links = check_links(parsed)
    log.info("  %d unique links, %d broken, %d unverified",
             links["unique_links"], links["broken_count"], links["unverified_count"])

    words = word_frequency(parsed)
    sentence_lengths = sentence_length_distribution(parsed)
    headings = story(parsed)
    layout = figure_layout(parsed)
    formatting = formatting_checks(parsed)
    log.info("Formatting: %d footnotes, %d footers, %d justified paragraphs",
             formatting["footnotes"], formatting["footers"],
             len(formatting["justified"]))
    log.info("Figure layout: %d multi-figure subsections, %d figures below "
             "%d%% column width, %d footers over %d lines",
             len(layout["multi_figure_subsections"]), len(layout["narrow_figures"]),
             90, len(layout.get("long_footers", [])), 2)
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

    # AI layer: does the heading sequence tell a convincing story?
    story_flag: dict = {}
    story_model = config.model_for("story flag")
    if headings and story_model and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            story_flag = run_story_flag(anthropic.Anthropic(), story_model, headings, config)
            log.info("Story flag (%s): %s - %s", story_model,
                     story_flag.get("flag"), story_flag.get("explanation"))
        except Exception as exc:  # noqa: BLE001 — health page works without AI
            log.warning("story flag failed (%s)", exc)
    else:
        log.warning("Skipping story flag (no model or API key configured)")

    # AI layer: per-title message flag (does each heading state a message?)
    message_model = config.model_for("message flag")
    if headings and message_model and os.environ.get("ANTHROPIC_API_KEY"):
        client = anthropic.Anthropic()
        counts = {"r": 0, "a": 0, "g": 0}
        for h in headings:
            try:
                flag, _ti, _to = run_message_flag(client, message_model, h["text"], config)
            except Exception as exc:  # noqa: BLE001
                log.warning("message flag failed for %r (%s)", h["text"][:40], exc)
                flag = ""
            h["message_flag"] = flag
            if flag in counts:
                counts[flag] += 1
        log.info("Per-title message flags (%s): r=%d a=%d g=%d",
                 message_model, counts["r"], counts["a"], counts["g"])
    else:
        log.warning("Skipping per-title message flag (no model or API key)")

    out = run_dir(doc_id) / "analysis.json"
    out.write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "document_type": parsed.document_type,
                "approx_pages_excl_annex": pages_excl_annex,
                "page_limit": config.page_limits.get(parsed.document_type, 0),
                "links": links,
                "word_frequency": words,
                "sentence_lengths": sentence_lengths,
                "story": headings,
                "story_flag": story_flag,
                "figure_layout": layout,
                "formatting": formatting,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    update_index(doc_id, parsed.title,
                 broken_links=links["broken_count"],
                 story_flag=story_flag.get("flag", ""))
    log.info("-> %s", out)
    if words:
        top = ", ".join(f"{w['word']} ({w['count']})" for w in words[:8])
        log.info("Top words: %s", top)


if __name__ == "__main__":
    main()
