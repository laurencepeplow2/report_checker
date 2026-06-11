"""No-AI document health analyses: broken links, overused words, story.

All three work on the parsed document only — no Claude calls.
"""
from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

from app.docs_parser import ParsedDocument

REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Connecting/function words excluded from the overuse count.
STOPWORDS = set("""
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

WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'’-]+")


def check_links(parsed: ParsedDocument) -> dict:
    """HTTP-check every unique external link. No AI involved."""
    by_url: dict[str, list[dict]] = {}
    for link in parsed.links:
        if link["url"].startswith(("http://", "https://")):
            by_url.setdefault(link["url"], []).append(link)

    def probe(url: str) -> dict:
        try:
            resp = requests.get(
                url, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            status: int | str = resp.status_code
            resp.close()
            broken = resp.status_code >= 400
        except requests.RequestException as exc:
            status = type(exc).__name__
            broken = True
        return {"url": url, "status": status, "broken": broken}

    with ThreadPoolExecutor(max_workers=8) as pool:
        probed = list(pool.map(probe, by_url))

    results = []
    for item in probed:
        occurrences = by_url[item["url"]]
        results.append({
            **item,
            "count": len(occurrences),
            "text": occurrences[0]["text"],
            "tabs": sorted({o["tab"] for o in occurrences}),
        })
    results.sort(key=lambda r: (not r["broken"], r["url"]))
    return {
        "total_links": sum(len(v) for v in by_url.values()),
        "unique_links": len(by_url),
        "broken_count": sum(1 for r in results if r["broken"]),
        "links": results,
    }


def word_frequency(parsed: ParsedDocument, top_n: int = 30) -> list[dict]:
    """Most-used words across paragraph chunks, connecting words removed."""
    counter: Counter[str] = Counter()
    for chunk in parsed.chunks:
        if chunk.input_level != "paragraph":
            continue
        for word in WORD_RE.findall(chunk.text.lower()):
            word = word.replace("’", "'").strip("'-")
            if len(word) > 2 and word not in STOPWORDS:
                counter[word] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top_n)]


def story(parsed: ParsedDocument) -> list[dict]:
    """Tab titles + section/sub-section headers in document order —
    read top to bottom: does this tell a story?"""
    return parsed.headings
