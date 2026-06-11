"""Parse a tabbed Google Doc into tagged chunks at three input levels.

The report template uses one document tab per section (Cover, Executive
Summary, numbered chapters, Annex). The Docs API only returns tab content
when ``includeTabsContent=true``; content lives under ``tabs[].documentTab``,
not the top-level body.

Rules:
- The Cover tab is discarded except for the ``document_type`` value it
  declares (all matching is lowercase).
- Every other tab is tagged with a ``section`` derived from its title.
- Chunks are emitted at three input levels:
    * ``paragraph``  — each non-empty body paragraph (tables collapse to one)
    * ``figure``     — each inline image, downloaded to data/images/
    * ``subsection`` — heading-bounded group of paragraphs + figures; a tab
      with no headings is one subsection
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.auth import docs_service

DEFAULT_DOCUMENT_TYPES = ["report", "briefing", "pr"]

# Section vocab comes from the master_report_checker config tab:
# cover / executive summary / main / annex. Cover is handled separately
# (discarded except document_type); every numbered chapter incl.
# recommendations is "main".
SECTION_RULES = [  # first match on the lowercased tab title wins
    ("executive summary", "executive summary"),
    ("annex", "annex"),
]
DEFAULT_SECTION = "main"


@dataclass
class Figure:
    figure_id: str
    image_path: str | None
    content_uri: str | None
    description: str  # alt text / title if the author set one


@dataclass
class Chunk:
    chunk_id: str
    input_level: str          # paragraph | figure | subsection
    document_type: str
    section: str
    tab_title: str
    heading_path: list[str]
    order: int
    text: str
    kind: str = "text"        # text | table | figure
    figures: list[Figure] = field(default_factory=list)


@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    document_type: str
    chunks: list[Chunk]

    def by_level(self, level: str) -> list[Chunk]:
        return [c for c in self.chunks if c.input_level == level]


def _paragraph_text(paragraph: dict) -> str:
    return "".join(
        el.get("textRun", {}).get("content", "")
        for el in paragraph.get("elements", [])
    )


def _table_text(table: dict) -> str:
    rows = []
    for row in table.get("tableRows", []):
        cells = []
        for cell in row.get("tableCells", []):
            cell_text = " ".join(
                _paragraph_text(el["paragraph"]).strip()
                for el in cell.get("content", [])
                if "paragraph" in el
            )
            cells.append(cell_text.strip())
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _heading_level(paragraph: dict) -> int | None:
    style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "")
    match = re.fullmatch(r"HEADING_(\d)", style)
    return int(match.group(1)) if match else None


def _flatten_tabs(tabs: list[dict]) -> list[dict]:
    flat = []
    for tab in tabs:
        flat.append(tab)
        flat.extend(_flatten_tabs(tab.get("childTabs", [])))
    return flat


def _section_for_tab(title: str) -> str:
    lowered = title.lower()
    for needle, section in SECTION_RULES:
        if needle in lowered:
            return section
    return DEFAULT_SECTION


def _extract_document_type(tab: dict, allowed_types: list[str]) -> str | None:
    """Scan Cover tab text for an allowed document type (all lowercase).

    Accepts either a bare value on its own line ("report") or a labelled
    form ("document_type: report").
    """
    body = tab.get("documentTab", {}).get("body", {}).get("content", [])
    lines: list[str] = []
    for element in body:
        if "paragraph" in element:
            text = _paragraph_text(element["paragraph"]).strip().lower()
            if text:
                lines.append(text)
        elif "table" in element:
            lines.extend(
                part.strip() for part in
                _table_text(element["table"]).lower().replace("|", "\n").splitlines()
            )
    for line in lines:
        cleaned = re.sub(r"^document[_ ]?type\s*[:\-]\s*", "", line).strip()
        if cleaned in allowed_types:
            return cleaned
    # fallback: substring match anywhere in the cover text
    blob = "\n".join(lines)
    for doc_type in allowed_types:
        if re.search(rf"\b{re.escape(doc_type)}\b", blob):
            return doc_type
    return None


def _download_image(uri: str, dest: Path) -> bool:
    try:
        resp = requests.get(uri, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except Exception:  # noqa: BLE001 — content URIs expire; missing image is non-fatal
        return False


def parse_document(
    doc_id: str,
    allowed_types: list[str] | None = None,
    image_dir: Path | None = None,
) -> ParsedDocument:
    allowed_types = [t.lower() for t in (allowed_types or DEFAULT_DOCUMENT_TYPES)]
    doc = docs_service().documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

    tabs = _flatten_tabs(doc.get("tabs", []))
    document_type = "unknown"
    chunks: list[Chunk] = []
    order = 0

    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "").strip()
        if title.lower() == "cover" or "cover" in title.lower():
            found = _extract_document_type(tab, allowed_types)
            if found:
                document_type = found
            continue  # cover content is otherwise discarded

        section = _section_for_tab(title)
        order = _parse_tab(tab, title, section, chunks, order, image_dir)

    for chunk in chunks:
        chunk.document_type = document_type

    return ParsedDocument(
        doc_id=doc_id,
        title=doc.get("title", ""),
        document_type=document_type,
        chunks=chunks,
    )


def _parse_tab(
    tab: dict,
    tab_title: str,
    section: str,
    chunks: list[Chunk],
    order: int,
    image_dir: Path | None,
) -> int:
    document_tab = tab.get("documentTab", {})
    body = document_tab.get("body", {}).get("content", [])
    inline_objects = document_tab.get("inlineObjects", {})
    tab_id = tab.get("tabProperties", {}).get("tabId", "tab")

    heading_stack: list[tuple[int, str]] = []  # (level, text)

    # Accumulator for the current subsection
    sub_paras: list[str] = []
    sub_figures: list[Figure] = []
    sub_heading_path: list[str] = []
    sub_index = 0

    def heading_path() -> list[str]:
        return [text for _, text in heading_stack]

    def flush_subsection() -> None:
        nonlocal sub_index, order
        if not sub_paras and not sub_figures:
            return
        chunks.append(Chunk(
            chunk_id=f"{tab_id}-sub-{sub_index}",
            input_level="subsection",
            document_type="",
            section=section,
            tab_title=tab_title,
            heading_path=list(sub_heading_path),
            order=order,
            text="\n\n".join(sub_paras),
            figures=list(sub_figures),
        ))
        order += 1
        sub_index += 1
        sub_paras.clear()
        sub_figures.clear()

    para_index = fig_index = 0
    for element in body:
        if "table" in element:
            text = _table_text(element["table"]).strip()
            if text:
                chunks.append(Chunk(
                    chunk_id=f"{tab_id}-para-{para_index}",
                    input_level="paragraph",
                    document_type="",
                    section=section,
                    tab_title=tab_title,
                    heading_path=heading_path(),
                    order=order,
                    text=text,
                    kind="table",
                ))
                order += 1
                para_index += 1
                sub_paras.append(text)
            continue

        if "paragraph" not in element:
            continue
        paragraph = element["paragraph"]
        level = _heading_level(paragraph)
        text = _paragraph_text(paragraph).strip()

        if level is not None:
            if text:
                # Every heading starts a new subsection.
                flush_subsection()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, text))
                sub_heading_path[:] = heading_path()
            continue

        # Figures embedded in this paragraph
        for el in paragraph.get("elements", []):
            obj_id = el.get("inlineObjectElement", {}).get("inlineObjectId")
            if not obj_id:
                continue
            embedded = (
                inline_objects.get(obj_id, {})
                .get("inlineObjectProperties", {})
                .get("embeddedObject", {})
            )
            uri = embedded.get("imageProperties", {}).get("contentUri")
            description = (
                embedded.get("title", "") + " " + embedded.get("description", "")
            ).strip()
            image_path = None
            if uri and image_dir is not None:
                image_dir.mkdir(parents=True, exist_ok=True)
                dest = image_dir / f"{tab_id}-fig-{fig_index}.png"
                if _download_image(uri, dest):
                    image_path = str(dest)
            figure = Figure(
                figure_id=f"{tab_id}-fig-{fig_index}",
                image_path=image_path,
                content_uri=uri,
                description=description,
            )
            chunks.append(Chunk(
                chunk_id=figure.figure_id,
                input_level="figure",
                document_type="",
                section=section,
                tab_title=tab_title,
                heading_path=heading_path(),
                order=order,
                text=description,
                kind="figure",
                figures=[figure],
            ))
            order += 1
            fig_index += 1
            sub_figures.append(figure)

        if text:
            chunks.append(Chunk(
                chunk_id=f"{tab_id}-para-{para_index}",
                input_level="paragraph",
                document_type="",
                section=section,
                tab_title=tab_title,
                heading_path=heading_path(),
                order=order,
                text=text,
            ))
            order += 1
            para_index += 1
            sub_paras.append(text)

    flush_subsection()
    return order
