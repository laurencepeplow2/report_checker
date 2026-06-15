"""No-AI document health analyses: broken links, overused words, story.

All three work on the parsed document only — no Claude calls.
"""
from __future__ import annotations

import csv
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from app.coded_checks import SENTENCE_SPLIT_RE
from app.docs_parser import ParsedDocument

# Domain words (T&E topics: transport, BEVs, standards...) that would always
# top the frequency list. User-editable; one word per row, lowercase.
WORD_EXCLUSIONS_CSV = Path(__file__).resolve().parent.parent / "word_exclusions.csv"

REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Small connecting/function words excluded from the overuse count.
_BASE_STOPWORDS = set("""
a about above across after again against all almost along already also
although always am among an and another any are around as at back be because
been before being below between both but by can cannot could did do does
doing down during each either else even ever every few for from further had
has have having he her here hers herself him himself his how however i if in
into is it its itself just less let like made make many may me might more
most much must my myself near neither never new no nor not now of off often
on once one only onto or other others ought our ours ourselves out over own
per rather same she should since so some still such than that the their
theirs them themselves then there therefore these they this those through
thus to too under until up upon us very was we were what when where whether
which while who whom whose why will with within without would yet you your
yours yourself yourselves it's don't doesn't isn't aren't won't can't e.g
i.e etc vs via
""".split())

# Discourse/transition and emphasis words we deliberately DO count —
# overusing these is a style habit worth seeing (e.g. "but", "however").
COUNTED_CONNECTIVES = set("""
but however although though while whereas yet therefore thus moreover
furthermore nevertheless nonetheless instead despite also still rather even
indeed often always never almost already only once significantly importantly
crucially ultimately overall
""".split())

STOPWORDS = _BASE_STOPWORDS - COUNTED_CONNECTIVES

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'’-]+")


# Statuses that mean "a bot blocked us", not "the link is dead" — a human
# in a browser (past the cookie wall / bot check) will usually get through.
BOT_BLOCK_STATUSES = {401, 403, 406, 429, 999}

PROBE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def check_links(parsed: ParsedDocument) -> dict:
    """HTTP-check every unique external link. No AI involved.

    Each link gets a state:
      ok         - responded < 400
      broken     - 404/410/5xx or connection/DNS failure
      unverified - timed out twice or blocked by bot protection;
                   needs a human click, NOT counted as broken
    """
    by_url: dict[str, list[dict]] = {}
    for link in parsed.links:
        if link["url"].startswith(("http://", "https://")):
            by_url.setdefault(link["url"], []).append(link)

    def probe(url: str) -> dict:
        for timeout in (REQUEST_TIMEOUT, REQUEST_TIMEOUT * 2):  # retry slow hosts
            try:
                resp = requests.get(
                    url, timeout=timeout, stream=True, allow_redirects=True,
                    headers=PROBE_HEADERS,
                )
                status: int | str = resp.status_code
                resp.close()
                if status in BOT_BLOCK_STATUSES:
                    return {"url": url, "status": status, "state": "unverified",
                            "note": "blocked by bot/cookie protection - open it manually"}
                if status >= 400:
                    return {"url": url, "status": status, "state": "broken", "note": ""}
                return {"url": url, "status": status, "state": "ok", "note": ""}
            except requests.Timeout:
                continue
            except requests.ConnectionError as exc:
                detail = str(exc)
                if "NameResolution" in detail or "getaddrinfo" in detail:
                    # domain does not resolve - definitively dead
                    return {"url": url, "status": "DNS failure", "state": "broken", "note": ""}
                # host exists but dropped the connection - typically bot
                # protection (e.g. mckinsey.com) rather than a dead link
                return {"url": url, "status": "ConnectionReset", "state": "unverified",
                        "note": "server dropped the automated request - open it manually"}
            except requests.RequestException as exc:
                return {"url": url, "status": type(exc).__name__, "state": "broken", "note": ""}
        return {"url": url, "status": "Timeout", "state": "unverified",
                "note": "timed out twice - open it manually"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        probed = list(pool.map(probe, by_url))

    state_rank = {"broken": 0, "unverified": 1, "ok": 2}
    results = []
    for item in probed:
        occurrences = by_url[item["url"]]
        results.append({
            **item,
            "broken": item["state"] == "broken",  # kept for compatibility
            "count": len(occurrences),
            "text": occurrences[0]["text"],
            "tabs": sorted({o["tab"] for o in occurrences}),
            "pages": sorted({o["page"] for o in occurrences if o.get("page")}),
        })
    results.sort(key=lambda r: (state_rank[r["state"]], r["url"]))
    return {
        "total_links": sum(len(v) for v in by_url.values()),
        "unique_links": len(by_url),
        "broken_count": sum(1 for r in results if r["state"] == "broken"),
        "unverified_count": sum(1 for r in results if r["state"] == "unverified"),
        "links": results,
    }


def load_word_exclusions() -> set[str]:
    """Domain words excluded from the overuse count (word_exclusions.csv)."""
    if not WORD_EXCLUSIONS_CSV.exists():
        return set()
    with WORD_EXCLUSIONS_CSV.open(encoding="utf-8-sig", newline="") as f:
        return {
            row["word"].strip().lower()
            for row in csv.DictReader(f)
            if row.get("word", "").strip()
        }


def word_frequency(parsed: ParsedDocument, top_n: int = 30) -> list[dict]:
    """Most-used words across paragraph chunks, with connecting words and
    T&E domain words (word_exclusions.csv) removed."""
    excluded = STOPWORDS | load_word_exclusions()
    counter: Counter[str] = Counter()
    for chunk in parsed.chunks:
        if chunk.input_level != "paragraph":
            continue
        for word in WORD_RE.findall(chunk.text.lower()):
            word = word.replace("’", "'").strip("'-")
            if len(word) > 2 and word not in excluded:
                counter[word] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top_n)]


# Word-count buckets for the sentence-length distribution (inclusive ranges).
SENTENCE_BUCKETS = [
    ("0-5", 0, 5), ("6-7", 6, 7), ("8-9", 8, 9), ("10-11", 10, 11),
    ("12-13", 12, 13), ("14-15", 14, 15), ("16-20", 16, 20),
    ("20+", 21, 10 ** 9),
]


def sentence_length_distribution(parsed: ParsedDocument) -> list[dict]:
    """Count words per sentence across every paragraph, bucketed for a tidy
    distribution bar chart. No AI."""
    counts = [0] * len(SENTENCE_BUCKETS)
    for chunk in parsed.chunks:
        if chunk.input_level != "paragraph":
            continue
        for sentence in SENTENCE_SPLIT_RE.split(chunk.text):
            words = len(sentence.split())
            if words == 0:
                continue
            for i, (_label, lo, hi) in enumerate(SENTENCE_BUCKETS):
                if lo <= words <= hi:
                    counts[i] += 1
                    break
    return [{"label": label, "count": c}
            for (label, _lo, _hi), c in zip(SENTENCE_BUCKETS, counts)]


# A real section/sub-section header starts with a number ("1. Value",
# "2.3 Lithium") or a roman-numeral compound used in the annex ("I.1
# Production and sales"). Heading-styled key-message statements and lines
# like "Key findings" don't, and are not part of the story.
SECTION_NUMBER_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+(?:\.\d+)+)[.)]?\s", re.IGNORECASE
)


# The story is the argument arc - the executive summary and the annex sit
# outside it, so they are excluded from this section.
STORY_EXCLUDED_TABS = {"executive summary", "annex"}


def story(parsed: ParsedDocument) -> list[dict]:
    """Tab titles + numbered section/sub-section headers in document order —
    read top to bottom: does this tell a story? Executive summary and annex
    headings are left out."""
    return [
        h for h in parsed.headings
        if (h["level"] == 0 or SECTION_NUMBER_RE.match(h["text"]))
        and h.get("tab", "").strip().lower() not in STORY_EXCLUDED_TABS
    ]


# A figure is "full column width" if it spans at least this share of the
# text column (page width minus margins).
FULL_WIDTH_THRESHOLD = 0.90


MAX_FOOTER_LINES = 2


def _chunk_heading(chunk) -> str:
    return chunk.heading_path[-1] if chunk.heading_path else chunk.tab_title


def _loc(chunk) -> dict:
    """Location fields so the UI can show "heading · ≈ p.N · Open in Docs"."""
    return {
        "tab": chunk.tab_title,
        "heading": _chunk_heading(chunk),
        "page": chunk.approx_page,
        "tab_id": chunk.tab_id,
        "heading_id": chunk.heading_id,
    }


def figure_layout(parsed: ParsedDocument) -> dict:
    """Deterministic layout checks on figures (no AI):
    - max one figure per sub-section
    - figures inserted at full column width
    - figure footers at most two lines (via local OCR)
    """
    multi = []
    for chunk in parsed.chunks:
        if chunk.input_level == "subsection" and len(chunk.figures) > 1:
            multi.append({**_loc(chunk), "figures": len(chunk.figures)})

    narrow = []
    column = parsed.column_width_pt
    if column:
        for chunk in parsed.chunks:
            if chunk.input_level != "figure" or not chunk.figures:
                continue
            width = chunk.figures[0].width_pt
            if width and width < FULL_WIDTH_THRESHOLD * column:
                narrow.append({**_loc(chunk), "figure_id": chunk.chunk_id,
                               "width_pt": round(width),
                               "pct_of_column": round(100 * width / column)})

    # Footer length via local OCR (the "coded" footer rule). Needs the
    # figure images on disk; silently skipped if OCR is unavailable.
    long_footers = []
    try:
        from app.figure_parts import extract_parts_path
        for chunk in parsed.chunks:
            if chunk.input_level != "figure" or not chunk.figures:
                continue
            image_path = chunk.figures[0].image_path
            if not image_path:
                continue
            try:
                footer = extract_parts_path(image_path).get("footer", {})
            except Exception:  # noqa: BLE001 — one bad image shouldn't kill the run
                continue
            if footer.get("lines", 0) > MAX_FOOTER_LINES:
                long_footers.append({**_loc(chunk), "figure_id": chunk.chunk_id,
                                     "lines": footer["lines"],
                                     "text": footer.get("text", "")[:160]})
    except ImportError:
        pass  # winocr not available on this platform

    return {
        "column_width_pt": round(column),
        "multi_figure_subsections": multi,
        "narrow_figures": narrow,
        "long_footers": long_footers,
    }


def formatting_checks(parsed: ParsedDocument) -> dict:
    """Document-level coded format checks (no AI): no footnotes/footers,
    body text left-aligned (not justified)."""
    justified = [
        {**_loc(c)} for c in parsed.chunks
        if c.input_level == "paragraph" and c.alignment == "justified"
    ]
    return {
        "footnotes": parsed.footnote_count,
        "footers": parsed.footer_count,
        "justified": justified,
    }
