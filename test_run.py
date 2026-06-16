"""Run the style rules over every report linked in the config tab.

Scope per report: every paragraph and figure chunk within the first
max_pages approximate pages (config "mode"/"max_pages"; 0 = whole
document), at the severity set in the config tab. One API call per
(chunk, rule), or one batch when config batching = yes; breached
non-figure chunks get a rewrite that sees the surrounding paragraphs
as context.

report_link may hold several Google Doc links - each gets its own
output folder data/runs/<doc_id>/ (test_run.csv, test_run.json), listed
in data/runs/index.json for the UI's report selector. A doc id passed
on the command line overrides the config links.

Usage:
    python test_run.py [doc_id]
"""
from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from app.check_engine import (
    build_check_params, build_rewrite_params, build_system, build_user_text,
    build_verify_params, cost_for, estimate_cost, estimate_run_cost,
    parse_check_message, parse_rewrite_message, parse_verify_message,
    restore_links, run_batch, run_check, run_rewrite, run_verification,
    usd_to_eur,
)
from app.checks import post_run_checks, preflight
from app.docs_parser import Chunk, parse_document
from app.runlog import setup_logging
from app.runs import run_dir, update_index
from app.styleguide import StyleGuideConfig, load_config, load_rules

log = logging.getLogger("report_checker.test_run")

FALLBACK_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"
FLAG_HISTORY_CSV = Path(__file__).resolve().parent / "flag_history.csv"
COST_LOG_CSV = Path(__file__).resolve().parent / "run_cost_log.csv"

load_dotenv()


def write_cost_log(row: dict) -> None:
    """Append one row per run: timestamp, file, per-step tokens and cost."""
    new_file = not COST_LOG_CSV.exists()
    with COST_LOG_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def write_verification_log(out_dir: Path, rows: list[dict]) -> None:
    """Per-run log of every r/a flag and whether the second pass kept it."""
    flagged = [r for r in rows if r["flag"] in ("r", "a")]
    with (out_dir / "verification_log.csv").open(
            "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "chunk_id", "section", "tab", "rule_id", "rule", "flag",
            "decision", "verdict", "reason", "quote"])
        writer.writeheader()
        for r in flagged:
            verdict = r.get("verdict", "")
            decision = "refuted" if verdict == "refuted" else "kept"
            writer.writerow({
                "chunk_id": r["chunk_id"], "section": r["section"],
                "tab": r["tab"], "rule_id": r["rule_id"], "rule": r["rule"],
                "flag": r["flag"], "decision": decision, "verdict": verdict,
                "reason": r.get("detail", ""), "quote": r.get("quote", ""),
            })


def update_flag_history(title: str, rows: list[dict]) -> None:
    """Per-rule red/amber counts for this document, appended as two new
    columns ("<title> (r)" / "<title> (a)") per report over time. Re-running
    the same report replaces its columns instead of duplicating them."""
    counts: dict[str, dict] = {}
    for row in rows:
        entry = counts.setdefault(row["rule_id"], {
            "rule_id": row["rule_id"], "category": row["category"],
            "rule": row["rule"], "r": 0, "a": 0,
        })
        if row["flag"] in ("r", "a"):
            entry[row["flag"]] += 1

    base_fields = ["rule_id", "category", "rule"]
    existing: dict[str, dict] = {}
    old_fields: list[str] = []
    if FLAG_HISTORY_CSV.exists():
        with FLAG_HISTORY_CSV.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            old_fields = [c for c in (reader.fieldnames or []) if c not in base_fields]
            for row in reader:
                existing[row["rule_id"]] = row

    col_r, col_a = f"{title} (r)", f"{title} (a)"
    fields = base_fields + [c for c in old_fields if c not in (col_r, col_a)] + [col_r, col_a]

    merged: dict[str, dict] = {}
    for rule_id, row in existing.items():
        merged[rule_id] = {c: row.get(c, "") for c in fields}
    for rule_id, entry in counts.items():
        row = merged.setdefault(rule_id, {c: "" for c in fields})
        row.update({
            "rule_id": entry["rule_id"], "category": entry["category"],
            "rule": entry["rule"], col_r: entry["r"], col_a: entry["a"],
        })

    with FLAG_HISTORY_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(merged[k] for k in sorted(merged))


def select_chunks(chunks: list[Chunk], max_pages: int) -> list[Chunk]:
    """Every paragraph and figure chunk within the first max_pages pages
    (0 = no page cap)."""
    return [
        c for c in chunks
        if c.input_level in ("paragraph", "figure")
        and (max_pages <= 0 or c.approx_page <= max_pages)
    ]


def _quote_in_text(quote: str, text: str) -> bool:
    """Whitespace-insensitive substring check, so a verifier quote only
    drives a highlight when it is genuinely present in the extract."""
    if not quote.strip():
        return False
    norm = lambda s: " ".join(s.split()).lower()
    return norm(quote) in norm(text)


def neighbour_context(chunks: list[Chunk], chunk: Chunk) -> tuple[str, str]:
    """Plain text of the paragraph before/after the chunk in the same tab."""
    paras = [c for c in chunks
             if c.input_level == "paragraph" and c.tab_title == chunk.tab_title]
    try:
        idx = next(i for i, c in enumerate(paras) if c.chunk_id == chunk.chunk_id)
    except StopIteration:
        return "", ""
    before = paras[idx - 1].text if idx > 0 else ""
    after = paras[idx + 1].text if idx + 1 < len(paras) else ""
    return before, after


def main() -> None:
    log_path = setup_logging("test_run")

    config = load_config()
    doc_ids = ([sys.argv[1]] if len(sys.argv) > 1
               else config.report_doc_ids or [FALLBACK_DOC_ID])
    rules = load_rules()

    ok = all(
        preflight(config=config, rules=rules, require_api_key=True, doc_id=d)
        if i == 0 else preflight(doc_id=d)
        for i, d in enumerate(doc_ids)
    )
    if not ok:
        sys.exit(1)

    severity = config.check_severity or "mid"
    log.info("Mode %s: %d report(s), severity %s, max %s pages, "
             "%d active rules, batching=%s cache=%s",
             config.mode or "(unnamed)", len(doc_ids), severity,
             config.max_pages or "all", len(rules),
             config.batching, config.cache)

    # extra retries so a transient 5xx/timeout doesn't abort a long run; any
    # call that still fails is skipped per-item in the loops below
    client = anthropic.Anthropic(max_retries=5)
    for doc_id in doc_ids:
        run_for_doc(client, config, rules, doc_id, severity)
    log.info("Full log: %s", log_path)


def coded_rule_kind(rule) -> str | None:
    """Map a coded paragraph rule to its deterministic checker."""
    t = rule.text.lower()
    if "sentence" in t and ("word" in t or "short" in t):
        return "sentence_length"
    if "transport & environment" in t or "transport and environment" in t \
            or 'full name' in t:
        return "org_name"
    return None


def coded_check_rows(config, rules, parsed, sample) -> list[dict]:
    """Per-paragraph deterministic checks shown in Review next to the AI
    flags - no API calls. Dispatches each coded paragraph rule to its
    checker (sentence length, full org name)."""
    from app.coded_checks import org_full_name_flag, sentence_length_flag

    coded_rules = [(r, coded_rule_kind(r)) for r in rules if r.coded]
    coded_rules = [(r, k) for r, k in coded_rules if k]
    if not coded_rules:
        return []

    rows = []
    for chunk in sample:
        if chunk.input_level != "paragraph" or chunk.kind == "table":
            continue
        for rule, kind in coded_rules:
            if not rule.applies_to(chunk.input_level, parsed.document_type,
                                   chunk.section):
                continue
            if kind == "sentence_length":
                if not config.sentence_word_limits:
                    continue
                flag, detail, quote = sentence_length_flag(
                    chunk.text, config.sentence_word_limits)
            elif kind == "org_name":
                flag, detail, quote = org_full_name_flag(chunk.text)
            else:
                continue
            rows.append({
                "chunk_id": chunk.chunk_id,
                "tab": chunk.tab_title,
                "section": chunk.section,
                "input_level": chunk.input_level,
                "chunk_text": chunk.text,
                "rule_id": rule.rule_id,
                "category": "",
                "rule": rule.text,
                "rule_tag": rule.rule_tag,
                "example": rule.example,
                "figure_type": "",
                "severity": "coded",
                "flag": flag,
                "raw_response": detail,
                # coded checks are deterministic - they self-verify, and the
                # matched text is the highlight quote
                "verdict": "confirmed" if flag in ("r", "a") else "",
                "quote": quote,
                "quotes": [],
                "detail": detail,
                "system_prompt": "",
                "user_prompt": "",
                "model": "coded",
                "input_tokens": 0,
                "output_tokens": 0,
            })
    return rows


def run_for_doc(
    client: anthropic.Anthropic,
    config: StyleGuideConfig,
    rules: list,
    doc_id: str,
    severity: str,
) -> None:
    model = config.model_for("rag report")
    rewrite_model = config.model_for("suggested improvement")

    parsed = parse_document(
        doc_id,
        allowed_types=config.document_types,
        image_dir=DATA_DIR / "images",
    )
    sample = select_chunks(parsed.chunks, config.max_pages)
    log.info("Document: %r (%s), %d chunks; %d in scope (first %s pages)",
             parsed.title, parsed.document_type, len(parsed.chunks),
             len(sample), config.max_pages or "all")
    system_prompt = build_system(severity, config)

    # Work list: every applicable (chunk, AI rule) pair (coded rules are
    # handled deterministically below, never sent to the AI). Rules marked
    # number_check only run on paragraphs that actually contain a number.
    from app.coded_checks import contains_number
    ai_rules = [r for r in rules if not r.coded]
    work: list[tuple[Chunk, object]] = []
    skipped_number = 0
    for chunk in sample:
        chunk_has_number = contains_number(chunk.text)
        for rule in ai_rules:
            if not rule.applies_to(chunk.input_level, parsed.document_type, chunk.section):
                continue
            if rule.number_check and chunk.input_level == "paragraph" \
                    and not chunk_has_number:
                skipped_number += 1
                continue
            work.append((chunk, rule))
    if skipped_number:
        log.info("number_check skipped %d (chunk, rule) pairs with no number",
                 skipped_number)
    log.info("%d checks to run (%s)", len(work),
             "Batches API, 50%% token cost" if config.batching else "serial")

    # ---- cost guard: estimate before spending, cap while spending ------
    verify_model = config.model_for("verification")
    cap_eur = config.max_report_cost_eur
    est = estimate_run_cost(work, system_prompt, config, model, verify_model,
                            rewrite_model)
    log.info("Estimated run cost ~$%.2f (~EUR %.2f) [flag $%.2f, verify $%.2f, "
             "rewrite $%.2f]%s", est["total"], usd_to_eur(est["total"]),
             est["flag"], est["verify"], est["rewrite"],
             f" | cap EUR {cap_eur}" if cap_eur else " | no cap")

    skip_ai = bool(cap_eur) and usd_to_eur(est["total"]) > cap_eur
    if skip_ai:
        log.warning("Estimated EUR %.2f exceeds the cap of EUR %d - skipping "
                    "ALL AI checks; coded checks only.",
                    usd_to_eur(est["total"]), cap_eur)
        work = []   # nothing goes to the AI; coded checks below still run

    # ---- first loop: flag checks --------------------------------------
    results: dict[tuple[str, str], object] = {}
    total_in = total_out = 0
    # per-step tokens for the cost log: [input, output]
    tok = {"flag": [0, 0], "verify": [0, 0], "rewrite": [0, 0]}
    spent_usd = 0.0
    stopped_early = False

    def over_cap() -> bool:
        return bool(cap_eur) and usd_to_eur(spent_usd) >= cap_eur

    if config.batching and work:
        # one batch - can't stop mid-flight, so the estimate is the guard here
        request_params = {
            f"chk-{i}": build_check_params(model, severity, rule, chunk, config)
            for i, (chunk, rule) in enumerate(work)
        }
        messages = run_batch(client, request_params)
        for i, (chunk, rule) in enumerate(work):
            results[(chunk.chunk_id, rule.rule_id)] = \
                parse_check_message(messages.get(f"chk-{i}"))
    elif work:
        for done, (chunk, rule) in enumerate(work, start=1):
            try:
                res = run_check(client, model, severity, rule, chunk, config)
            except anthropic.APIError as exc:
                log.warning("  %s x %s -> API error (%s); skipping this check",
                            chunk.chunk_id, rule.rule_id, type(exc).__name__)
                continue
            results[(chunk.chunk_id, rule.rule_id)] = res
            spent_usd += cost_for(model, res.input_tokens, res.output_tokens)
            log.info("  %s x %s -> %s", chunk.chunk_id, rule.rule_id, res.flag)
            if over_cap():
                stopped_early = True
                log.warning("Hit cost cap EUR %d (spent ~EUR %.2f) - stopping "
                            "AI after %d/%d flag checks; writing partial "
                            "results.", cap_eur, usd_to_eur(spent_usd),
                            done, len(work))
                break
    # only the (chunk, rule) pairs that actually got a flag carry downstream
    work = [(c, r) for (c, r) in work if (c.chunk_id, r.rule_id) in results]
    # once the cap is hit (or AI was skipped), no further AI spend is allowed
    ai_budget_left = not skip_ai and not stopped_early

    rows = []
    breached_by_chunk: dict[str, list] = {}
    for chunk, rule in work:
        result = results[(chunk.chunk_id, rule.rule_id)]
        total_in += result.input_tokens
        total_out += result.output_tokens
        tok["flag"][0] += result.input_tokens
        tok["flag"][1] += result.output_tokens
        if result.flag in ("r", "a"):
            breached_by_chunk.setdefault(chunk.chunk_id, []).append(rule)
        rows.append({
            "chunk_id": chunk.chunk_id,
            "tab": chunk.tab_title,
            "section": chunk.section,
            "input_level": chunk.input_level,
            "chunk_text": chunk.text,
            "rule_id": rule.rule_id,
            "category": rule.category,
            "rule": rule.text,
            "rule_tag": rule.rule_tag,
            "example": rule.example,
            "figure_type": rule.figure_type,
            "severity": severity,
            "flag": result.flag,
            "raw_response": result.raw_response,
            "verdict": "",   # filled by the verification pass below
            "quote": "",
            "quotes": [],
            "detail": "",
            "system_prompt": system_prompt,
            "user_prompt": build_user_text(rule, chunk),
            "model": model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        })

    # In batch mode the flag batch runs in one shot, so the cap is checked
    # between stages: if the flags alone already reached it, skip verify/rewrite.
    if config.batching and ai_budget_left:
        spent_usd = cost_for(model, tok["flag"][0], tok["flag"][1])
        if over_cap():
            ai_budget_left = False
            log.warning("Flag batch already cost ~EUR %.2f (cap EUR %d) - "
                        "skipping verify and rewrite.",
                        usd_to_eur(spent_usd), cap_eur)

    # ---- coded checks (no AI): sentence length ------------------------
    coded_rows = coded_check_rows(config, rules, parsed, sample)
    rules_by_id = {r.rule_id: r for r in rules}
    for row in coded_rows:
        rows.append(row)
        if row["flag"] in ("r", "a"):
            breached_by_chunk.setdefault(row["chunk_id"], []).append(
                rules_by_id[row["rule_id"]])
    if coded_rows:
        coded_flags = [r["flag"] for r in coded_rows]
        log.info("Coded sentence-length check: %d paragraphs (r=%d a=%d)",
                 len(coded_rows), coded_flags.count("r"), coded_flags.count("a"))

    # ---- verification pass: independent confirm/refute + offending quote
    if config.verify and ai_budget_left:
        verify_work = [(c, r) for (c, r) in work
                       if results[(c.chunk_id, r.rule_id)].flag in ("r", "a")]
        log.info("Verifying %d AI flags (%s)", len(verify_work),
                 "batch" if config.batching else "serial")
        verdicts: dict[tuple[str, str], object] = {}
        if config.batching and verify_work:
            req = {f"vf-{i}": build_verify_params(verify_model, r, c, config)
                   for i, (c, r) in enumerate(verify_work)}
            msgs = run_batch(client, req)
            for i, (c, r) in enumerate(verify_work):
                verdicts[(c.chunk_id, r.rule_id)] = parse_verify_message(msgs.get(f"vf-{i}"))
        else:
            for c, r in verify_work:
                try:
                    verdicts[(c.chunk_id, r.rule_id)] = \
                        run_verification(client, verify_model, r, c, config)
                except anthropic.APIError as exc:
                    log.warning("  verify %s x %s -> API error (%s); leaving "
                                "unverified", c.chunk_id, r.rule_id,
                                type(exc).__name__)
        # attach to the AI rows, validating the quote is really in the text
        text_by_chunk = {c.chunk_id: c.text for c in sample}
        confirmed = refuted = 0
        for row in rows:
            v = verdicts.get((row["chunk_id"], row["rule_id"]))
            if v is None:
                continue
            row["verdict"] = v.verdict
            row["detail"] = v.note
            row["quote"] = v.quote if _quote_in_text(
                v.quote, text_by_chunk.get(row["chunk_id"], "")) else ""
            total_in += v.input_tokens
            total_out += v.output_tokens
            tok["verify"][0] += v.input_tokens
            tok["verify"][1] += v.output_tokens
            confirmed += v.verdict == "confirmed"
            refuted += v.verdict == "refuted"
        log.info("Verification: %d confirmed, %d refuted", confirmed, refuted)

    # ---- deterministic highlight for the bold/underline rule ----------
    # The verifier rarely quotes the emphasised span, so pin it from the
    # formatting itself - the card then highlights only the bold/underlined
    # text (longest span = the main offender).
    from app.coded_checks import emphasis_spans, is_emphasis_rule
    fmt_by_chunk = {c.chunk_id: (c.formatted_text or c.text) for c in sample}
    for row in rows:
        if (row["flag"] in ("r", "a") and row.get("verdict") != "refuted"
                and is_emphasis_rule(row.get("rule", ""))):
            spans = emphasis_spans(fmt_by_chunk.get(row["chunk_id"], ""))
            if spans:
                seen = set()
                uniq = [s for s in spans if not (s in seen or seen.add(s))]
                row["quotes"] = uniq               # highlight every bold span
                row["quote"] = max(uniq, key=len)   # longest = primary (CSV/compat)

    # ---- second loop: rewrites for breached non-figure chunks ---------
    # (an AI call, so skipped once the cost cap is reached / AI was skipped)
    suggestions: dict[str, str] = {}
    rewrite_work = [
        (chunk, breached_by_chunk[chunk.chunk_id])
        for chunk in sample
        if chunk.chunk_id in breached_by_chunk and chunk.input_level != "figure"
    ] if ai_budget_left else []
    log.info("%d chunks need rewrites", len(rewrite_work))
    if config.batching and rewrite_work:
        request_params = {}
        for i, (chunk, breached) in enumerate(rewrite_work):
            before, after = neighbour_context(sample, chunk)
            request_params[f"rw-{i}"] = build_rewrite_params(
                rewrite_model, breached, chunk, config, before, after)
        messages = run_batch(client, request_params)
        for i, (chunk, breached) in enumerate(rewrite_work):
            rewrite = parse_rewrite_message(messages.get(f"rw-{i}"))
            total_in += rewrite.input_tokens
            total_out += rewrite.output_tokens
            tok["rewrite"][0] += rewrite.input_tokens
            tok["rewrite"][1] += rewrite.output_tokens
            if rewrite.suggestion:
                suggestions[chunk.chunk_id] = restore_links(
                    chunk.formatted_text or chunk.text, rewrite.suggestion)
    else:
        for chunk, breached in rewrite_work:
            before, after = neighbour_context(sample, chunk)
            try:
                rewrite = run_rewrite(client, rewrite_model, breached, chunk,
                                      config, context_before=before,
                                      context_after=after)
            except anthropic.APIError as exc:
                log.warning("  rewrite %s -> API error (%s); skipping",
                            chunk.chunk_id, type(exc).__name__)
                continue
            total_in += rewrite.input_tokens
            total_out += rewrite.output_tokens
            tok["rewrite"][0] += rewrite.input_tokens
            tok["rewrite"][1] += rewrite.output_tokens
            suggestions[chunk.chunk_id] = restore_links(
                chunk.formatted_text or chunk.text, rewrite.suggestion)
            log.info("  rewrite %s (%d breached rules) -> %d chars",
                     chunk.chunk_id, len(breached), len(rewrite.suggestion))

    for row in rows:
        row["suggestion"] = suggestions.get(row["chunk_id"], "")

    out_dir = run_dir(doc_id)
    out_path = out_dir / "test_run.csv"
    if not rows:
        log.warning("No rows produced (AI skipped and no coded breaches) - "
                    "writing an empty test_run.csv.")
    fieldnames = list(rows[0].keys()) if rows else ["chunk_id", "rule_id", "flag"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # JSON for the reviewer UI: chunks in document order with their results
    by_chunk: dict[str, dict] = {}
    for chunk in sample:
        by_chunk[chunk.chunk_id] = {
            "chunk_id": chunk.chunk_id,
            "tab": chunk.tab_title,
            "section": chunk.section,
            "input_level": chunk.input_level,
            "heading_path": chunk.heading_path,
            "approx_page": chunk.approx_page,
            "tab_id": chunk.tab_id,
            "heading_id": chunk.heading_id,
            "text": chunk.text,
            "kind": chunk.kind,
            "formatted_text": chunk.formatted_text or chunk.text,
            "image": (chunk.figures[0].image_path if chunk.figures else None),
            "suggestion": suggestions.get(chunk.chunk_id, ""),
            "results": [],
        }
    for row in rows:
        by_chunk[row["chunk_id"]]["results"].append({
            "rule_id": row["rule_id"],
            "category": row["category"],
            "rule": row["rule"],
            "rule_tag": row.get("rule_tag", ""),
            "flag": row["flag"],
            "verdict": row.get("verdict", ""),
            "quote": row.get("quote", ""),
            "quotes": row.get("quotes", []),
            "detail": row.get("detail", ""),
        })
    (out_dir / "test_run.json").write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "document_type": parsed.document_type,
                "severity": severity,
                "mode": config.mode,
                "model": model,
                "run_date": datetime.now().strftime("%d/%m/%Y"),
                "chunks": list(by_chunk.values()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    flags = [r["flag"] for r in rows]
    update_flag_history(parsed.title, rows)

    # ---- verification log: what each flag's second pass kept / refuted
    verify_model = config.model_for("verification")
    write_verification_log(out_dir, rows)

    # ---- cost log: one appended row per run, with a per-step breakdown
    refuted = sum(1 for r in rows if r.get("verdict") == "refuted")
    step_models = {"flag": model, "verify": verify_model, "rewrite": rewrite_model}
    cost_flag = cost_for(model, *tok["flag"])
    cost_verify = cost_for(verify_model, *tok["verify"])
    cost_rewrite = cost_for(rewrite_model, *tok["rewrite"])
    cost_total = cost_flag + cost_verify + cost_rewrite
    write_cost_log({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "file": parsed.title,
        "doc_id": doc_id,
        "mode": config.mode,
        "severity": severity,
        "checks": len(rows),
        "red": flags.count("r"),
        "amber": flags.count("a"),
        "green": flags.count("g"),
        "refuted": refuted,
        "rewrites": len(suggestions),
        "flag_model": model,
        "flag_in": tok["flag"][0], "flag_out": tok["flag"][1], "flag_cost": round(cost_flag, 4),
        "verify_model": verify_model,
        "verify_in": tok["verify"][0], "verify_out": tok["verify"][1], "verify_cost": round(cost_verify, 4),
        "rewrite_model": rewrite_model,
        "rewrite_in": tok["rewrite"][0], "rewrite_out": tok["rewrite"][1], "rewrite_cost": round(cost_rewrite, 4),
        "total_in": total_in, "total_out": total_out, "total_cost": round(cost_total, 4),
    })

    update_index(doc_id, parsed.title, clear_others=config.clear_ui,
                 mode=config.mode, severity=severity,
                 checks=len(rows), red=flags.count("r"), amber=flags.count("a"),
                 refuted=refuted, cost=round(cost_total, 4))
    log.info("Flag history -> %s | cost log -> %s",
             FLAG_HISTORY_CSV.name, COST_LOG_CSV.name)

    log.info("%d checks -> %s (+ test_run.json)", len(rows), out_path)
    log.info("Flags: r=%d a=%d g=%d invalid=%d (refuted by verify: %d)",
             flags.count("r"), flags.count("a"), flags.count("g"),
             flags.count("invalid"), refuted)
    log.info("Tokens: %d in / %d out | cost ~$%.4f (flag $%.4f, verify $%.4f, "
             "rewrite $%.4f)", total_in, total_out, cost_total,
             cost_flag, cost_verify, cost_rewrite)

    post_run_checks(parsed, rows, suggestions)

    # publish the at-a-glance pass/fail summary to the sheet's automated_checks
    # tab (best-effort: needs Editor access, never aborts the run)
    try:
        from app.automated_checks import write_automated_checks
        write_automated_checks(doc_id, config)
    except Exception as exc:  # noqa: BLE001
        log.warning("automated_checks update skipped: %s", exc)


if __name__ == "__main__":
    main()
