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


# A real section/sub-section header starts with a number ("1. Value",
# "2.3 Lithium") or a roman-numeral compound used in the annex ("I.1
# Production and sales"). Heading-styled key-message statements and lines
# like "Key findings" don't, and are not part of the story.
SECTION_NUMBER_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[IVXLCDM]+(?:\.\d+)+)[.)]?\s", re.IGNORECASE
)


def story(parsed: ParsedDocument) -> list[dict]:
    """Tab titles + numbered section/sub-section headers in document order —
    read top to bottom: does this tell a story?"""
    return [
        h for h in parsed.headings
        if h["level"] == 0 or SECTION_NUMBER_RE.match(h["text"])
    ]
