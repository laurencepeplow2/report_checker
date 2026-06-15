"""Prove the live-doc commit path actually changes text, then restore.

Picks a real paragraph chunk, applies a marked edit via doc_editor,
re-fetches the doc to confirm the new text is present, then restores the
original text. Leaves the document pristine.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.doc_editor import apply_edit, find_paragraph, _paragraph_text, _normalise
from app.auth import docs_service

RUN = (Path(__file__).resolve().parent.parent / "data" / "runs"
       / "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8" / "test_run.json")


def doc_contains(doc_id: str, text: str) -> bool:
    doc = docs_service().documents().get(
        documentId=doc_id, includeTabsContent=True).execute()
    target = _normalise(text)

    def walk(tabs):
        for tab in tabs:
            body = tab.get("documentTab", {}).get("body", {}).get("content", [])
            for el in body:
                if "paragraph" in el:
                    yield _normalise(_paragraph_text(el["paragraph"]))
            yield from walk(tab.get("childTabs", []))
    return any(target in t for t in walk(doc.get("tabs", [])))


def main() -> None:
    data = json.loads(RUN.read_text(encoding="utf-8"))
    doc_id = data["doc_id"]
    chunk = next(
        c for c in data["chunks"]
        if c["input_level"] == "paragraph" and " | " not in c["text"]
        and 60 < len(c["text"]) < 300
    )
    original = chunk["text"]
    print(f"chunk: {chunk['chunk_id']}\noriginal: {original[:90]!r}")

    # confirm we can locate it
    loc = find_paragraph(doc_id, chunk.get("tab_id", ""), original)
    print(f"located at index {loc.start}-{loc.end}, tab {loc.tab_id}")

    # apply a real, visible change with a bold run + a normal run
    edited = "MARKER " + original
    apply_edit(doc_id, chunk.get("tab_id", ""), original, [
        {"text": "MARKER ", "bold": True},
        {"text": original},
    ])
    landed = doc_contains(doc_id, "MARKER " + original[:40])
    print(f"edit landed in doc: {landed}")

    # restore the original (direct, bypassing the UI's one-edit lock)
    apply_edit(doc_id, chunk.get("tab_id", ""), edited, [{"text": original}])
    restored = doc_contains(doc_id, original[:60]) and not doc_contains(
        doc_id, "MARKER " + original[:40])
    print(f"restored to original: {restored}")
    print("RESULT:", "PASS" if landed and restored else "FAIL")


if __name__ == "__main__":
    main()
