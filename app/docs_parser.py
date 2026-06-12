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
# cover / executive summary / main text / annex. Cover is handled separately
# (discarded except document_type); every numbered chapter incl.
# recommendations is "main text".
SECTION_RULES = [  # first match on the lowercased tab title wins
    ("executive summary", "executive summary"),
    ("annex", "annex"),
]
DEFAULT_SECTION = "main text"

# A numbered section header: "1. Value", "2.3 Lithium", annex "I.1 ...".
SECTION_NUMBER_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+(?:\.\d+)+)[.)]?\s", re.IGNORECASE
)

# Rough characters per printed page, used for the approximate page number
# shown in the UI (the Docs API exposes no real pagination).
CHARS_PER_PAGE = 2600
FIGURE_CHAR_EQUIVALENT = 1200


def _is_pseudo_heading(text: str) -> int | None:
    """Some sub-headers are styled as normal text (e.g. "1.3 European EV
    production falls sharply..."). Treat a short numbered line as a heading;
    its level is the depth of the leading number ("1.3" -> 2)."""
    if len(text) > 130 or not SECTION_NUMBER_RE.match(text):
        return None
    number = text.split()[0].rstrip(".)")
    return min(number.count(".") + 1, 4)


@dataclass
class Figure:
    figure_id: str
    image_path: str | None
    content_uri: str | None
    description: str  # alt text / title if the author set one
    width_pt: float = 0.0  # rendered width in points, for layout checks


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
    tab_id: str = ""          # for deep links into the Google Doc
    heading_id: str = ""      # nearest heading anchor (#heading=...)
    approx_page: int = 0      # rough printed-page estimate
    formatted_text: str = ""  # text with **bold**, *italic*, <u>..</u>, [link](url)


@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    document_type: str
    chunks: list[Chunk]
    links: list[dict] = field(default_factory=list)     # {url, text, tab}
    headings: list[dict] = field(default_factory=list)  # {tab, level, text} in doc order
    column_width_pt: float = 0.0  # page width minus margins, for layout checks

    def by_level(self, level: str) -> list[Chunk]:
        return [c for c in self.chunks if c.input_level == level]


def _paragraph_text(paragraph: dict) -> str:
    return "".join(
        el.get("textRun", {}).get("content", "")
        for el in paragraph.get("elements", [])
    )


def _run_markup(run: dict) -> str:
    """One text run with its formatting marked inline: **bold**, *italic*,
    <u>underlined</u>, [link text](url). Some style rules depend on this."""
    content = run.get("content", "")
    stripped = content.rstrip("\n")
    trail = content[len(stripped):]
    if not stripped.strip():
        return content
    style = run.get("textStyle", {})
    url = style.get("link", {}).get("url")
    if style.get("bold"):
        stripped = f"**{stripped}**"
    if style.get("italic"):
        stripped = f"*{stripped}*"
    if style.get("underline") and not url:  # links are underlined by default
        stripped = f"<u>{stripped}</u>"
    if url:
        stripped = f"[{stripped}]({url})"
    return stripped + trail


def _paragraph_formatted(paragraph: dict) -> str:
    return "".join(
        _run_markup(el["textRun"])
        for el in paragraph.get("elements", [])
        if "textRun" in el
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


def _paragraph_links(paragraph: dict, tab_title: str) -> list[dict]:
    found = []
    for el in paragraph.get("elements", []):
        run = el.get("textRun", {})
        url = run.get("textStyle", {}).get("link", {}).get("url")
        if url:
            found.append({
                "url": url,
                "text": run.get("content", "").strip(),
                "tab": tab_title,
            })
    return found


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
    links: list[dict] = []
    headings: list[dict] = []
    column_width_pt = 0.0
    order = 0

    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "").strip()
        if title.lower() == "cover" or "cover" in title.lower():
            found = _extract_document_type(tab, allowed_types)
            if found:
                document_type = found
            continue  # cover content is otherwise discarded

        section = _section_for_tab(title)
        if not column_width_pt:
            doc_style = tab.get("documentTab", {}).get("documentStyle", {})
            page = doc_style.get("pageSize", {}).get("width", {}).get("magnitude", 0)
            margins = (doc_style.get("marginLeft", {}).get("magnitude", 0)
                       + doc_style.get("marginRight", {}).get("magnitude", 0))
            column_width_pt = max(page - margins, 0)
        order = _parse_tab(tab, title, section, chunks, order, image_dir,
                           links, headings)

    chars = 0
    for chunk in chunks:  # appended in document order
        chunk.document_type = document_type
        chunk.approx_page = chars // CHARS_PER_PAGE + 1
        if chunk.input_level == "paragraph":
            chars += len(chunk.text) + 60  # spacing overhead
        elif chunk.input_level == "figure":
            chars += FIGURE_CHAR_EQUIVALENT

    return ParsedDocument(
        doc_id=doc_id,
        title=doc.get("title", ""),
        document_type=document_type,
        chunks=chunks,
        links=links,
        headings=headings,
        column_width_pt=column_width_pt,
    )


def _parse_tab(
    tab: dict,
    tab_title: str,
    section: str,
    chunks: list[Chunk],
    order: int,
    image_dir: Path | None,
    links: list[dict],
    headings: list[dict],
) -> int:
    document_tab = tab.get("documentTab", {})
    body = document_tab.get("body", {}).get("content", [])
    inline_objects = document_tab.get("inlineObjects", {})
    tab_id = tab.get("tabProperties", {}).get("tabId", "tab")

    headings.append({"tab": tab_title, "level": 0, "text": tab_title})

    heading_stack: list[tuple[int, str]] = []  # (level, text)
    state = {"heading_id": ""}  # nearest real heading anchor for deep links

    # Accumulator for the current subsection
    sub_paras: list[str] = []
    sub_paras_fmt: list[str] = []
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
            formatted_text="\n\n".join(sub_paras_fmt),
            figures=list(sub_figures),
            tab_id=tab_id,
            heading_id=state["heading_id"],
        ))
        order += 1
        sub_index += 1
        sub_paras.clear()
        sub_paras_fmt.clear()
        sub_figures.clear()

    para_index = fig_index = 0
    for element in body:
        if "table" in element:
            for row in element["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    for cell_el in cell.get("content", []):
                        if "paragraph" in cell_el:
                            links.extend(_paragraph_links(cell_el["paragraph"], tab_title))
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
                    tab_id=tab_id,
                    heading_id=state["heading_id"],
                    formatted_text=text,
                ))
                order += 1
                para_index += 1
                sub_paras.append(text)
                sub_paras_fmt.append(text)
            continue

        if "paragraph" not in element:
            continue
        paragraph = element["paragraph"]
        links.extend(_paragraph_links(paragraph, tab_title))
        level = _heading_level(paragraph)
        text = _paragraph_text(paragraph).strip()

        if level is None and text:
            # Sub-headers styled as normal text ("1.3 European EV production
            # falls sharply...") are headings in disguise.
            level = _is_pseudo_heading(text)

        if level is not None:
            if text:
                # Every heading starts a new subsection.
                flush_subsection()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, text))
                sub_heading_path[:] = heading_path()
                headings.append({"tab": tab_title, "level": level, "text": text})
                heading_anchor = paragraph.get("paragraphStyle", {}).get("headingId")
                if heading_anchor:
                    state["heading_id"] = heading_anchor
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
            width_pt = embedded.get("size", {}).get("width", {}).get("magnitude", 0.0)
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
                width_pt=width_pt,
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
                tab_id=tab_id,
                heading_id=state["heading_id"],
            ))
            order += 1
            fig_index += 1
            sub_figures.append(figure)

        if text:
            formatted = _paragraph_formatted(paragraph).strip()
            chunks.append(Chunk(
                chunk_id=f"{tab_id}-para-{para_index}",
                input_level="paragraph",
                document_type="",
                section=section,
                tab_title=tab_title,
                heading_path=heading_path(),
                order=order,
                text=text,
                formatted_text=formatted,
                tab_id=tab_id,
                heading_id=state["heading_id"],
            ))
            order += 1
            para_index += 1
            sub_paras.append(text)
            sub_paras_fmt.append(formatted)

    flush_subsection()
    return order
