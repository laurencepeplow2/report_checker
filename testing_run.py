"""Development test harness - genuinely tries to break the pipeline.

Run:  .venv\\Scripts\\python.exe testing_run.py

Prints a timestamped PASS/FAIL report grouped into sections, then a summary
and a non-zero exit code if anything fails. It is fully deterministic: NO AI
calls and NO network (run with `--live-links` to also probe a few real URLs).
It works on synthetic fixtures with known outcomes - the point is to catch
regressions, not to look pretty.

We DON'T run this on every report; it's for development. IMPORTANT: whenever we
add or change code, add tests here so the harness keeps genuinely testing the
model. Sections:
  A text cleaning / content      F links (extraction + classification)
  B section mapping              G footers / footnotes
  C tab parsing / ingestion      H distributions + word frequency
  D coded rules                  I story arc
  E rule loading + routing       J rewrites / prompt / cost
                                 K figures (OCR, loose; needs sample images)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from app import analysis as A
from app import coded_checks as C
from app import docs_parser as D
from app import check_engine as E
from app.docs_parser import Chunk, Figure, ParsedDocument
from app.styleguide import Rule, load_rules

ROOT = Path(__file__).resolve().parent
RESULTS: list[tuple[str, str, bool, str]] = []


def ok(section: str, name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((section, name, bool(cond), "" if cond else detail))


def eq(section: str, name: str, got, expected) -> None:
    RESULTS.append((section, name, got == expected,
                    "" if got == expected else f"got {got!r}, expected {expected!r}"))


# ---- Docs-API-shaped fixture builders (no Google calls) -------------------
def run(text, *, bold=False, italic=False, underline=False, link=None):
    style = {}
    if bold:
        style["bold"] = True
    if italic:
        style["italic"] = True
    if underline:
        style["underline"] = True
    if link:
        style["link"] = {"url": link}
    return {"textRun": {"content": text, "textStyle": style}}


def para(*runs, heading=None, align=None):
    pstyle = {}
    if heading:
        pstyle["namedStyleType"] = f"HEADING_{heading}"
    if align:
        pstyle["alignment"] = align
    return {"paragraph": {"elements": list(runs), "paragraphStyle": pstyle}}


def table(*rowtexts):
    rows = [{"tableCells": [{"content": [para(run(c))]} for c in row]} for row in rowtexts]
    return {"table": {"tableRows": rows}}


def tab(title, content, tab_id="t.test"):
    return {"tabProperties": {"title": title, "tabId": tab_id},
            "documentTab": {"body": {"content": content}}}


def parse_tab(tabdict, section="main text", heading_sections=False):
    chunks, links, headings = [], [], []
    D._parse_tab(tabdict, tabdict["tabProperties"]["title"], section,
                 chunks, 0, None, links, headings, heading_sections=heading_sections)
    return chunks, links, headings


def seg(*texts):
    return {"content": [para(run(t)) for t in texts]}


def doc_with(chunks=(), headings=(), footnotes=0, footers=0, col_width=510.0):
    return ParsedDocument(doc_id="d", title="t", document_type="report",
                          chunks=list(chunks), links=[], headings=list(headings),
                          column_width_pt=col_width, footnote_count=footnotes,
                          footer_count=footers)


def pchunk(text, *, section="main text", alignment="", level=2, heading="X"):
    return Chunk(chunk_id="c", input_level="paragraph", document_type="report",
                 section=section, tab_title="t", heading_path=[heading], order=0,
                 text=text, formatted_text=text, alignment=alignment)


# ===========================================================================
def section_A():
    s = "A. text cleaning / content"
    eq(s, "clean strips object-replacement char", D._clean_text("a￼b"), "ab")
    eq(s, "clean strips replacement char", D._clean_text("a�b"), "ab")
    eq(s, "clean strips zero-width space", D._clean_text("a​b"), "ab")
    eq(s, "clean keeps normal text", D._clean_text("hello world"), "hello world")
    ok(s, "has_content true for letters", D._has_content("abc"))
    ok(s, "has_content true for a number", D._has_content("100"))
    ok(s, "has_content false for em-dash", not D._has_content("—"))
    ok(s, "has_content false for bullets", not D._has_content("• •"))
    ok(s, "has_content false for blank", not D._has_content("   "))
    eq(s, "paragraph_text joins runs", D._paragraph_text(para(run("a "), run("b"))["paragraph"]), "a b")


def section_B():
    s = "B. section mapping"
    cases = {
        "Executive Summary": "executive summary", "1. Value": "main text",
        "3. Components": "main text", "Annex": "annex", "Appendix A": "annex",
        "Methodology": "annex", "5. Methodology": "annex", "Bibliography": "annex",
        "Acknowledgements": "annex", "References": "annex", "Foreword": "foreward",
        "6. Recommendations": "recommendations", "Recommendations": "recommendations",
        "Cover": "main text",  # 'cover' handled separately in parse_document, not here
    }
    for title, expected in cases.items():
        eq(s, f"{title!r} -> {expected}", D._section_for_tab(title), expected)


def section_C():
    s = "C. tab parsing / ingestion"
    body = [para(run("First paragraph has five words.")),
            para(run("Second paragraph also has some words here.")),
            para(run("—")),                         # divider, must be skipped
            table(["A", "B"], ["1", "2"])]
    chunks, links, headings = parse_tab(tab("1. Intro", body))
    paras = [c for c in chunks if c.input_level == "paragraph" and c.kind != "table"]
    eq(s, "two real paragraphs (em-dash skipped)", len(paras), 2)
    # word count preserved through ingestion
    src_words = len("First paragraph has five words.".split()) + \
        len("Second paragraph also has some words here.".split())
    got_words = sum(len(c.text.split()) for c in paras)
    eq(s, "word count preserved before/after ingestion", got_words, src_words)
    ok(s, "table detected (kind=table)", any(c.kind == "table" for c in chunks))
    ok(s, "table cells joined with ' | '", any(" | " in c.text for c in chunks if c.kind == "table"))

    # bold / link markup carried into formatted_text
    chunks2, _, _ = parse_tab(tab("1. Intro", [para(run("see "), run("France", link="https://x.fr/a"), run(" now"))]))
    pc = next(c for c in chunks2 if c.input_level == "paragraph")
    ok(s, "link markup [label](url) in formatted_text", "[France](https://x.fr/a)" in pc.formatted_text)
    chunks3, _, _ = parse_tab(tab("1. Intro", [para(run("a "), run("bold bit", bold=True))]))
    ok(s, "bold markup ** in formatted_text", "**bold bit**" in chunks3[0].formatted_text)
    chunks4, _, _ = parse_tab(tab("1. Intro", [para(run("justified text here"), align="JUSTIFIED")]))
    eq(s, "justified alignment captured", chunks4[0].alignment, "justified")

    # single-tab: sections come from H1 headings; front matter before first H1 skipped
    body = [para(run("REPORT")), para(run("Cover subtitle line")),
            para(run("Executive summary", ), heading=1),
            para(run("Exec body text here")),
            para(run("1. Introduction"), heading=1),
            para(run("Intro body text")),
            para(run("Methodology"), heading=1),
            para(run("Methods body"))]
    chunks, _, headings = parse_tab(tab("Report w/ cover image", body), heading_sections=True)
    secs = {c.section for c in chunks if c.input_level == "paragraph"}
    ok(s, "single-tab: exec summary section detected", "executive summary" in secs)
    ok(s, "single-tab: main text section detected", "main text" in secs)
    ok(s, "single-tab: methodology -> annex", "annex" in secs)
    ok(s, "single-tab: front matter (pre-H1) skipped",
       not any("Cover subtitle" in c.text for c in chunks))
    ok(s, "single-tab: tab title NOT added as heading",
       not any(h["text"] == "Report w/ cover image" for h in headings))

    # multi-tab style: section is the passed-in tab section, headings carry it
    chunks, _, headings = parse_tab(tab("Annex", [para(run("annex para"))]), section="annex")
    ok(s, "multi-tab: chunk takes tab section", all(c.section == "annex" for c in chunks))
    ok(s, "multi-tab: tab title heading present", any(h["text"] == "Annex" for h in headings))


def section_D():
    s = "D. coded rules"
    limits = {"a": 12, "r": 16}
    eq(s, "short sentence -> green", C.sentence_length_flag("Three short words.", limits)[0], "g")
    long13 = "one two three four five six seven eight nine ten eleven twelve thirteen."
    eq(s, "13-word sentence -> amber", C.sentence_length_flag(long13, limits)[0], "a")
    long18 = " ".join(["word"] * 18) + "."
    eq(s, "18-word sentence -> red", C.sentence_length_flag(long18, limits)[0], "r")
    ok(s, "sentence-length returns the offending sentence",
       C.sentence_length_flag(long18, limits)[2].startswith("word word"))
    eq(s, "org full name -> red", C.org_full_name_flag("By Transport & Environment today")[0], "r")
    eq(s, "org full name quote captured",
       C.org_full_name_flag("By Transport & Environment today")[2], "Transport & Environment")
    eq(s, "org 'and' variant -> red", C.org_full_name_flag("Transport and Environment")[0], "r")
    eq(s, "T&E abbrev -> green", C.org_full_name_flag("By T&E today")[0], "g")
    ok(s, "contains_number: numeral", C.contains_number("we saw 42 cases"))
    ok(s, "contains_number: number word", C.contains_number("about three cases"))
    ok(s, "contains_number: none", not C.contains_number("no quantities here"))
    ok(s, "contains_hyperlink: yes", C.contains_hyperlink("see [x](https://a.com/b)"))
    ok(s, "contains_hyperlink: no", not C.contains_hyperlink("plain text only"))
    spans = C.emphasis_spans("**The** quick **brown fox** and <u>lazy dog</u>")
    eq(s, "emphasis_spans count", len(spans), 3)
    ok(s, "emphasis_spans gets bold phrase", "brown fox" in spans)
    ok(s, "emphasis_spans gets underline", "lazy dog" in spans)
    ok(s, "emphasis_spans ignores non-alnum", "**!!**" not in spans)
    ok(s, "is_emphasis_rule true for bold", C.is_emphasis_rule("Do not use bold for emphasis"))
    ok(s, "is_emphasis_rule false otherwise", not C.is_emphasis_rule("Use short sentences"))


def section_E():
    s = "E. rule loading + routing"
    try:
        rules = load_rules()
    except Exception as exc:  # noqa: BLE001
        ok(s, "load_rules succeeds", False, str(exc)[:120])
        return
    ai = [r for r in rules if not r.coded]
    ok(s, "has AI rules", len(ai) > 0, f"{len(ai)} AI rules")
    ok(s, "has coded rules", len(rules) - len(ai) > 0)
    ok(s, "all rules have a level", all(r.input_level in ("paragraph", "figure", "subsection") for r in rules))
    ok(s, "rule levels valid", {r.input_level for r in rules} <= {"paragraph", "figure", "subsection"})
    ok(s, "every AI rule has rule_tag", all(r.rule_tag for r in ai))
    ok(s, "some rules carry right/wrong examples", any(r.right or r.wrong for r in ai))
    ok(s, "hyperlink rules exist", any(r.hyperlink_rule for r in rules))
    # routing predicates (the gating test_run uses)
    figure_rule = next((r for r in rules if r.input_level == "figure"), None)
    if figure_rule:
        ok(s, "figure rule applies to figure level",
           figure_rule.applies_to("figure", next(iter(figure_rule.document_types), "report"),
                                   next(iter(figure_rule.sections), "main text")))
        ok(s, "figure rule does NOT apply to paragraph",
           not figure_rule.applies_to("paragraph", "report", "main text"))
    # "only hyperlinks go to hyperlink questions"
    hrule = next((r for r in rules if r.hyperlink_rule), None)
    if hrule:
        with_link = C.contains_hyperlink("text [a](https://b.com/c) more")
        without = C.contains_hyperlink("text without a link")
        ok(s, "hyperlink rule gated IN on link chunk", with_link)
        ok(s, "hyperlink rule gated OUT on no-link chunk", not without)
    # number_check gating
    nrule = next((r for r in rules if r.number_check), None)
    if nrule:
        ok(s, "number_check gated IN on number chunk", C.contains_number("up 12%"))
        ok(s, "number_check gated OUT on no-number chunk", not C.contains_number("none here"))


def section_F():
    s = "F. links"
    # extraction
    p = para(run("see "), run("anchor text", link="https://site.com/p"))["paragraph"]
    links = D._paragraph_links(p, "1. Value")
    eq(s, "link extracted count", len(links), 1)
    eq(s, "link url", links[0]["url"], "https://site.com/p")
    eq(s, "link anchor text", links[0]["text"], "anchor text")
    eq(s, "link tab recorded", links[0]["tab"], "1. Value")
    ok(s, "no link -> none", D._paragraph_links(para(run("plain"))["paragraph"], "x") == [])
    # classification of known broken-link structures
    eq(s, "200 -> ok", A.classify_http_status(200)[0], "ok")
    eq(s, "204 -> ok", A.classify_http_status(204)[0], "ok")
    eq(s, "301 -> ok", A.classify_http_status(301)[0], "ok")
    eq(s, "404 -> broken", A.classify_http_status(404)[0], "broken")
    eq(s, "410 -> broken", A.classify_http_status(410)[0], "broken")
    eq(s, "500 -> broken", A.classify_http_status(500)[0], "broken")
    eq(s, "503 -> broken", A.classify_http_status(503)[0], "broken")
    eq(s, "403 -> unverified (bot wall)", A.classify_http_status(403)[0], "unverified")
    eq(s, "429 -> unverified (rate limit)", A.classify_http_status(429)[0], "unverified")
    eq(s, "401 -> unverified", A.classify_http_status(401)[0], "unverified")


def section_G():
    s = "G. footers / footnotes"
    eq(s, "empty footers count 0", D._nonempty_segments({"a": seg(""), "b": seg("   ")}), 0)
    eq(s, "one footer with text", D._nonempty_segments({"a": seg("Source: TE 2026")}), 1)
    eq(s, "two non-empty of three", D._nonempty_segments({"a": seg("x"), "b": seg(""), "c": seg("y")}), 2)
    eq(s, "no segments -> 0", D._nonempty_segments({}), 0)
    eq(s, "None -> 0", D._nonempty_segments(None), 0)
    # formatting_checks reads the counts + justified chunks
    parsed = doc_with(chunks=[pchunk("normal"), pchunk("just", alignment="justified")],
                      footnotes=2, footers=1)
    fc = A.formatting_checks(parsed)
    eq(s, "formatting_checks footnotes", fc["footnotes"], 2)
    eq(s, "formatting_checks footers", fc["footers"], 1)
    eq(s, "formatting_checks justified count", len(fc["justified"]), 1)


def section_H():
    s = "H. distributions + word frequency"
    parsed = doc_with(chunks=[pchunk("Short. " + " ".join(["w"] * 12) + ". " + " ".join(["w"] * 25) + ".")])
    dist = {d["label"]: d["count"] for d in A.sentence_length_distribution(parsed)}
    ok(s, "sentence dist has buckets", "0-5" in dist and "20+" in dist)
    ok(s, "sentence dist counts the 25-word sentence as 20+", dist["20+"] >= 1)
    bands = {d["label"]: d["band"] for d in A.sentence_length_distribution(parsed)}
    eq(s, "0-5 band green", bands["0-5"], "g")
    eq(s, "20+ band darker red", bands["20+"], "dr")
    wl = {d["label"]: d["count"] for d in A.word_length_distribution(
        doc_with(chunks=[pchunk("a bb cccc dddddddddd")]))}  # 1,2,4,10
    ok(s, "word-length buckets present", set(wl) == {"1-4", "5-6", "7-8", "9-10", "11+"})
    ok(s, "short words counted in 1-4", wl["1-4"] >= 2)
    ok(s, "10-letter word in 9-10", wl["9-10"] >= 1)
    # word frequency excludes stopwords + domain words
    freq = A.word_frequency(doc_with(chunks=[pchunk("battery battery the the and however however")]))
    words = {w["word"] for w in freq}
    ok(s, "stopword 'the' excluded", "the" not in words)
    ok(s, "content word 'however' kept", "however" in words)


def section_I():
    s = "I. story arc"
    headings = [
        {"tab": "Foreword", "level": 1, "text": "Foreword", "section": "foreward"},
        {"tab": "Exec", "level": 1, "text": "Executive summary", "section": "executive summary"},
        {"tab": "1. Intro", "level": 1, "text": "1. Introduction", "section": "main text"},
        {"tab": "1. Intro", "level": 2, "text": "1.1 Background", "section": "main text"},
        {"tab": "Conc", "level": 1, "text": "4. Conclusion", "section": "main text"},
        {"tab": "Recs", "level": 1, "text": "Recommendations", "section": "recommendations"},
        {"tab": "Annex", "level": 1, "text": "Annex", "section": "annex"},
    ]
    parsed = doc_with(headings=headings)
    disp = [h["text"] for h in A.story(parsed, for_display=True)]
    arc = [h["text"] for h in A.story(parsed, for_display=False)]
    ok(s, "display excludes Foreword", "Foreword" not in disp)
    ok(s, "display excludes Recommendations", "Recommendations" not in disp)
    ok(s, "display excludes 4. Conclusion", "4. Conclusion" not in disp)
    ok(s, "display excludes Executive summary", "Executive summary" not in disp)
    ok(s, "display excludes Annex", "Annex" not in disp)
    ok(s, "display keeps 1. Introduction", "1. Introduction" in disp)
    ok(s, "arc keeps Recommendations", "Recommendations" in arc)
    ok(s, "arc keeps 4. Conclusion", "4. Conclusion" in arc)
    ok(s, "arc still excludes Executive summary", "Executive summary" not in arc)
    ok(s, "arc still excludes Annex", "Annex" not in arc)
    eq(s, "_story_name strips number", A._story_name("4. Conclusion"), "conclusion")
    eq(s, "_story_name strips roman", A._story_name("I.2 Methods"), "methods")


def section_J():
    s = "J. rewrites / prompt / cost"
    o = "We saw [in France](https://tf1.fr/a) and [in Spain](https://b.es/c)."
    eq(s, "restore_links re-wraps dropped link",
       E.restore_links(o, "We saw in France and in Spain."),
       "We saw [in France](https://tf1.fr/a) and [in Spain](https://b.es/c).")
    ok(s, "restore_links leaves already-linked",
       E.restore_links(o, "As [in France](https://tf1.fr/a) shown.").count("[in France]") == 1)
    eq(s, "restore_links: reworded anchor untouched",
       E.restore_links(o, "We saw elsewhere."), "We saw elsewhere.")
    ok(s, "restore_links substring-safe",
       "[EU](" in E.restore_links("[EU](https://eu.int)", "The EUlogy and the EU rules") and
       "EUlogy" in E.restore_links("[EU](https://eu.int)", "The EUlogy and the EU rules"))
    blk = E._rule_block(Rule("r1", "", "Do X", "paragraph", right="good eg", wrong="bad eg"))
    ok(s, "rule block shows COMPLIES example", "COMPLIES" in blk and "good eg" in blk)
    ok(s, "rule block shows BREACHES example", "BREACHES" in blk and "bad eg" in blk)
    eq(s, "eur<->usd round trip", round(E.usd_to_eur(E.eur_to_usd(10)), 2), 10.0)
    est = E.estimate_run_cost([], "sys", None, "claude-haiku-4-5", "claude-opus-4-8", "claude-opus-4-8")
    eq(s, "estimate for empty work is 0", est["total"], 0.0)


def section_K():
    s = "K. figures (OCR, loose)"
    imgs = sorted((ROOT / "data" / "images").glob("*.png")) if (ROOT / "data" / "images").exists() else []
    if not imgs:
        ok(s, "sample figure images present (skipped if none)", True, "no images - skipped")
        return
    try:
        parts = E.figure_parts_for(str(imgs[0]))
        ok(s, "figure_parts returns a dict", isinstance(parts, dict))
        ok(s, "figure_parts has footer line count", isinstance(parts.get("footer_lines", 0), int))
    except Exception as exc:  # noqa: BLE001
        ok(s, "figure_parts runs on a sample image", False, str(exc)[:120])


SECTIONS = [section_A, section_B, section_C, section_D, section_E,
            section_F, section_G, section_H, section_I, section_J, section_K]

TESTING_TAB = "testing_run"


def upload_results(ts: str) -> None:
    """Write the run to the master sheet's `testing_run` tab (created if it
    doesn't exist). Best-effort - needs Editor access."""
    from app.auth import sheets_service
    from app.styleguide import find_sheet_id
    total = len(RESULTS)
    n_fail = sum(1 for r in RESULTS if not r[2])
    overall = "FAIL" if n_fail else "PASS"
    header = [
        ["Testing run", "", f"updated {ts}"],
        ["OVERALL", overall, f"{total - n_fail}/{total} passed, {n_fail} failed"],
        ["", "", ""],
        ["Section", "Check", "Result", "Detail"],
    ]
    body = [[sec, name, "PASS" if passed else "FAIL", detail]
            for sec, name, passed, detail in RESULTS]
    values = header + body

    svc = sheets_service().spreadsheets()
    sid = find_sheet_id()
    meta = svc.get(spreadsheetId=sid).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if TESTING_TAB not in tabs:
        svc.batchUpdate(spreadsheetId=sid, body={
            "requests": [{"addSheet": {"properties": {"title": TESTING_TAB}}}]
        }).execute()
    svc.values().clear(spreadsheetId=sid, range=f"'{TESTING_TAB}'!A1:D2000").execute()
    svc.values().update(spreadsheetId=sid, range=f"'{TESTING_TAB}'!A1",
                        valueInputOption="RAW", body={"values": values}).execute()
    print(f"\nUploaded {total} results to the '{TESTING_TAB}' tab.")


def main() -> None:
    ts = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    print("=" * 72)
    print(f"  REPORT CHECKER - test harness   {ts}")
    print("=" * 72)
    for fn in SECTIONS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a crash in one section is a fail, not the end
            RESULTS.append((fn.__name__, "section crashed", False, repr(exc)))

    current = None
    for section, name, passed, detail in RESULTS:
        if section != current:
            current = section
            print(f"\n{section}")
        mark = "PASS" if passed else "FAIL"
        line = f"  [{mark}] {name}"
        if not passed and detail:
            line += f"   <- {detail}"
        print(line)

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[2]]
    print("\n" + "=" * 72)
    print(f"  {total - len(failed)}/{total} passed"
          + (f"   {len(failed)} FAILED" if failed else "   ALL PASS"))
    print("=" * 72)

    if "--upload" in sys.argv:
        try:
            upload_results(ts)
        except Exception as exc:  # noqa: BLE001
            print(f"!! upload failed (needs Editor access on the sheet): {exc}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
