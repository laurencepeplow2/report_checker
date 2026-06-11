"""Phase 1 CLI: download a Google Doc report and segment it into chunks.

Usage:
    python ingest.py [doc_id]

Writes data/chunks.json (+ figure images under data/images/) and prints a
summary of what was parsed.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from app.docs_parser import DEFAULT_DOCUMENT_TYPES, parse_document
from app.styleguide import StyleGuideConfig, load_config

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else TEST_DOC_ID

    try:
        config = load_config()
        print(f"Loaded config: model={config.claude_model!r}, "
              f"severity={config.check_severity!r}, "
              f"document_types={config.document_types}")
    except Exception as exc:  # noqa: BLE001
        config = StyleGuideConfig(document_types=DEFAULT_DOCUMENT_TYPES)
        print(f"WARNING: could not load master_report_checker config ({exc}); "
              f"using default document types {DEFAULT_DOCUMENT_TYPES}")

    parsed = parse_document(
        doc_id,
        allowed_types=config.document_types or DEFAULT_DOCUMENT_TYPES,
        image_dir=DATA_DIR / "images",
    )

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / "chunks.json"
    out_path.write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "document_type": parsed.document_type,
                "chunks": [asdict(c) for c in parsed.chunks],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"\nDocument: {parsed.title!r}")
    print(f"document_type (from Cover tab): {parsed.document_type}")
    print(f"Total chunks: {len(parsed.chunks)} -> {out_path}")

    by_level = Counter(c.input_level for c in parsed.chunks)
    print(f"  by input_level: {dict(by_level)}")

    by_tab = Counter((c.tab_title, c.section) for c in parsed.chunks)
    print("  by tab:")
    for (tab, section), count in by_tab.items():
        print(f"    {tab!r:45} section={section!r:22} {count} chunks")


if __name__ == "__main__":
    main()
