"""Document health analyses: broken links, overused words, story.

Links / word counts / story need no AI. One optional AI step classifies
which overused words are a style issue (model: config "Overused words").

Usage:
    python analyse_doc.py [doc_id]

Writes data/analysis.json for the UI.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from app.analysis import check_links, story, word_frequency
from app.check_engine import run_word_flagging
from app.docs_parser import DEFAULT_DOCUMENT_TYPES, parse_document
from app.styleguide import StyleGuideConfig, load_config

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"

load_dotenv()


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else TEST_DOC_ID
    try:
        config = load_config()
    except Exception:  # noqa: BLE001
        config = StyleGuideConfig(document_types=DEFAULT_DOCUMENT_TYPES)
    allowed = config.document_types or DEFAULT_DOCUMENT_TYPES

    parsed = parse_document(doc_id, allowed_types=allowed)
    print(f"{parsed.title!r}: {len(parsed.links)} links, "
          f"{len(parsed.headings)} headings")

    print("Checking links (HTTP)...")
    links = check_links(parsed)
    print(f"  {links['unique_links']} unique links, {links['broken_count']} broken")

    words = word_frequency(parsed)
    headings = story(parsed)

    # AI step: which overused words are a style issue (vs subject matter)?
    word_model = config.model_for("overused words")
    if words and word_model and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            flagged = run_word_flagging(anthropic.Anthropic(), word_model, words, config)
            for w in words:
                w["flagged"] = w["word"] in flagged
                w["reason"] = flagged.get(w["word"], "")
            print(f"AI-flagged words ({word_model}): "
                  + ", ".join(sorted(flagged)) if flagged else "none")
        except Exception as exc:  # noqa: BLE001 — health page works without AI
            print(f"WARNING: word flagging failed ({exc}); words left unflagged")
    else:
        print("Skipping AI word flagging (no model or API key configured)")

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / "analysis.json"
    out.write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "links": links,
                "word_frequency": words,
                "story": headings,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"-> {out}")
    if words:
        top = ", ".join(f"{w['word']} ({w['count']})" for w in words[:8])
        print(f"Top words: {top}")


if __name__ == "__main__":
    main()
