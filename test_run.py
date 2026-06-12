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
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from app.check_engine import (
    build_check_params, build_rewrite_params, build_system, build_user_text,
    estimate_cost, parse_check_message, parse_rewrite_message, run_batch,
    run_check, run_rewrite,
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

load_dotenv()


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

    client = anthropic.Anthropic()
    for doc_id in doc_ids:
        run_for_doc(client, config, rules, doc_id, severity)
    log.info("Full log: %s", log_path)


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

    # Work list: every applicable (chunk, rule) pair
    work: list[tuple[Chunk, object]] = []
    for chunk in sample:
        for rule in rules:
            if rule.applies_to(chunk.input_level, parsed.document_type, chunk.section):
                work.append((chunk, rule))
    log.info("%d checks to run (%s)", len(work),
             "Batches API, 50%% token cost" if config.batching else "serial")

    # ---- first loop: flag checks --------------------------------------
    results: dict[tuple[str, str], object] = {}
    total_in = total_out = 0
    if config.batching:
        request_params = {
            f"chk-{i}": build_check_params(model, severity, rule, chunk, config)
            for i, (chunk, rule) in enumerate(work)
        }
        messages = run_batch(client, request_params)
        for i, (chunk, rule) in enumerate(work):
            results[(chunk.chunk_id, rule.rule_id)] = \
                parse_check_message(messages.get(f"chk-{i}"))
    else:
        for chunk, rule in work:
            results[(chunk.chunk_id, rule.rule_id)] = \
                run_check(client, model, severity, rule, chunk, config)
            log.info("  %s x %s -> %s", chunk.chunk_id, rule.rule_id,
                     results[(chunk.chunk_id, rule.rule_id)].flag)

    rows = []
    breached_by_chunk: dict[str, list] = {}
    for chunk, rule in work:
        result = results[(chunk.chunk_id, rule.rule_id)]
        total_in += result.input_tokens
        total_out += result.output_tokens
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
            "example": rule.example,
            "figure_type": rule.figure_type,
            "severity": severity,
            "flag": result.flag,
            "raw_response": result.raw_response,
            "system_prompt": system_prompt,
            "user_prompt": build_user_text(rule, chunk),
            "model": model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        })

    # ---- second loop: rewrites for breached non-figure chunks ---------
    suggestions: dict[str, str] = {}
    rewrite_work = [
        (chunk, breached_by_chunk[chunk.chunk_id])
        for chunk in sample
        if chunk.chunk_id in breached_by_chunk and chunk.input_level != "figure"
    ]
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
            if rewrite.suggestion:
                suggestions[chunk.chunk_id] = rewrite.suggestion
    else:
        for chunk, breached in rewrite_work:
            before, after = neighbour_context(sample, chunk)
            rewrite = run_rewrite(client, rewrite_model, breached, chunk, config,
                                  context_before=before, context_after=after)
            total_in += rewrite.input_tokens
            total_out += rewrite.output_tokens
            suggestions[chunk.chunk_id] = rewrite.suggestion
            log.info("  rewrite %s (%d breached rules) -> %d chars",
                     chunk.chunk_id, len(breached), len(rewrite.suggestion))

    for row in rows:
        row["suggestion"] = suggestions.get(row["chunk_id"], "")

    out_dir = run_dir(doc_id)
    out_path = out_dir / "test_run.csv"
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
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
            "image": (chunk.figures[0].image_path if chunk.figures else None),
            "suggestion": suggestions.get(chunk.chunk_id, ""),
            "results": [],
        }
    for row in rows:
        by_chunk[row["chunk_id"]]["results"].append({
            "rule_id": row["rule_id"],
            "category": row["category"],
            "rule": row["rule"],
            "flag": row["flag"],
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
                "chunks": list(by_chunk.values()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    flags = [r["flag"] for r in rows]
    update_flag_history(parsed.title, rows)
    update_index(doc_id, parsed.title, mode=config.mode, severity=severity,
                 checks=len(rows), red=flags.count("r"), amber=flags.count("a"))
    log.info("Flag history updated -> %s", FLAG_HISTORY_CSV.name)

    log.info("%d checks -> %s (+ test_run.json)", len(rows), out_path)
    log.info("Flags: r=%d a=%d g=%d invalid=%d",
             flags.count("r"), flags.count("a"), flags.count("g"),
             flags.count("invalid"))
    log.info("Tokens: %d in / %d out (~$%.4f)",
             total_in, total_out, estimate_cost(total_in, total_out))

    post_run_checks(parsed, rows, suggestions)


if __name__ == "__main__":
    main()
