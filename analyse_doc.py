"""Document health analyses (no AI): broken links, overused words, story.

Usage:
    python analyse_doc.py [doc_id]

Writes data/analysis.json for the UI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.analysis import check_links, story, word_frequency
from app.docs_parser import DEFAULT_DOCUMENT_TYPES, parse_document
from app.styleguide import load_config

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"

load_dotenv()


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else TEST_DOC_ID
    try:
        allowed = load_config().document_types or DEFAULT_DOCUMENT_TYPES
    except Exception:  # noqa: BLE001
        allowed = DEFAULT_DOCUMENT_TYPES

    parsed = parse_document(doc_id, allowed_types=allowed)
    print(f"{parsed.title!r}: {len(parsed.links)} links, "
          f"{len(parsed.headings)} headings")

    print("Checking links (HTTP)...")
    links = check_links(parsed)
    print(f"  {links['unique_links']} unique links, {links['broken_count']} broken")

    words = word_frequency(parsed)
    headings = story(parsed)

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
