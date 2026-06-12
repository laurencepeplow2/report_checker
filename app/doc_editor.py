"""Apply reviewed suggestions back to the Google Doc as live edits.

The reviewer edits the suggestion in the UI (with bold / italic /
underline / hyperlinks), then commits. We re-fetch the document, locate
the paragraph whose text still matches the chunk's original text, and
replace it in one batchUpdate: delete + insert + per-run text styling.

If the paragraph's text changed since the check run (someone edited the
doc), the match fails and we refuse rather than guessing - the run is
stale at that point.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.auth import docs_service

_WS = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass
class ParagraphLocation:
    start: int
    end: int       # endIndex of the paragraph element (incl. trailing \n)
    tab_id: str


class EditError(Exception):
    """User-facing reasons a commit could not be applied."""


def _paragraph_text(paragraph: dict) -> str:
    return "".join(
        el.get("textRun", {}).get("content", "")
        for el in paragraph.get("elements", [])
    )


def find_paragraph(doc_id: str, tab_id: str, original_text: str) -> ParagraphLocation:
    doc = docs_service().documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

    def walk(tabs):
        for tab in tabs:
            yield tab
            yield from walk(tab.get("childTabs", []))

    target = _normalise(original_text)
    for tab in walk(doc.get("tabs", [])):
        if tab.get("tabProperties", {}).get("tabId") != tab_id:
            continue
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        for element in body:
            if "paragraph" not in element:
                continue
            if _normalise(_paragraph_text(element["paragraph"])) == target:
                return ParagraphLocation(
                    start=element["startIndex"],
                    end=element["endIndex"],
                    tab_id=tab_id,
                )
        raise EditError(
            "Could not find this paragraph in the document - its text has "
            "probably been edited since this check run. Re-run the checks "
            "to refresh."
        )
    raise EditError("The document tab for this extract no longer exists.")


def build_requests(location: ParagraphLocation, runs: list[dict]) -> list[dict]:
    """delete + insert + styling requests for one paragraph replacement.

    runs: [{"text": str, "bold": bool, "italic": bool, "underline": bool,
            "link": str|""}] in order.
    """
    new_text = "".join(r.get("text", "") for r in runs)
    if not new_text.strip():
        raise EditError("The edited text is empty - nothing to commit.")
    # keep the paragraph's trailing newline: delete up to end-1
    requests: list[dict] = [
        {"deleteContentRange": {"range": {
            "startIndex": location.start,
            "endIndex": location.end - 1,
            "tabId": location.tab_id,
        }}},
        {"insertText": {
            "location": {"index": location.start, "tabId": location.tab_id},
            "text": new_text,
        }},
    ]

    offset = location.start
    for run in runs:
        text = run.get("text", "")
        if not text:
            continue
        start, end = offset, offset + len(text)
        offset = end
        style: dict = {
            "bold": bool(run.get("bold")),
            "italic": bool(run.get("italic")),
            "underline": bool(run.get("underline")),
        }
        fields = "bold,italic,underline"
        if run.get("link"):
            style["link"] = {"url": run["link"]}
            fields += ",link"
        requests.append({"updateTextStyle": {
            "range": {"startIndex": start, "endIndex": end,
                      "tabId": location.tab_id},
            "textStyle": style,
            "fields": fields,
        }})
    return requests


def apply_edit(doc_id: str, tab_id: str, original_text: str,
               runs: list[dict]) -> dict:
    location = find_paragraph(doc_id, tab_id, original_text)
    requests = build_requests(location, runs)
    docs_service().documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute()
    new_text = "".join(r.get("text", "") for r in runs)
    return {"new_text": new_text, "start": location.start}
