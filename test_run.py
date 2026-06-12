"""Test mode: run the style rules over the report.

Scope: every paragraph and figure chunk within the first MAX_PAGES
approximate pages, at TEST_SEVERITY. One API call per (chunk, rule);
breached non-figure chunks get a rewrite that sees the surrounding
paragraphs as context. Results land in data/test_run.csv with the exact
prompts that were sent.

The document comes from the config tab's report_link unless a doc id is
passed on the command line.

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
    build_system, build_user_text, estimate_cost, run_check, run_rewrite,
)
from app.checks import post_run_checks, preflight
from app.docs_parser import Chunk, parse_document
from app.runlog import setup_logging
from app.styleguide import load_config, load_rules

log = logging.getLogger("report_checker.test_run")

FALLBACK_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"
FLAG_HISTORY_CSV = Path(__file__).resolve().parent / "flag_history.csv"
TEST_SEVERITY = "mid"   # forced in test mode
MAX_PAGES = 10          # only check chunks within the first ~N pages

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


def select_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """Every paragraph and figure chunk within the first MAX_PAGES pages."""
    return [
        c for c in chunks
        if c.input_level in ("paragraph", "figure") and c.approx_page <= MAX_PAGES
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
    doc_id = (sys.argv[1] if len(sys.argv) > 1
              else config.report_doc_id or FALLBACK_DOC_ID)
    model = config.model_for("rag report")
    rewrite_model = config.model_for("suggested improvement")
    rules = load_rules()

    if not preflight(config=config, rules=rules, require_api_key=True, doc_id=doc_id):
        sys.exit(1)

    log.info("Models: checks=%s rewrites=%s | severity: %s (forced) | "
             "%d active rules | first %d pages",
             model, rewrite_model, TEST_SEVERITY, len(rules), MAX_PAGES)

    parsed = parse_document(
        doc_id,
        allowed_types=config.document_types,
        image_dir=DATA_DIR / "images",
    )
    sample = select_chunks(parsed.chunks)
    log.info("Document: %r (%s), %d chunks; %d in scope (first %d pages)",
             parsed.title, parsed.document_type, len(parsed.chunks),
             len(sample), MAX_PAGES)

    client = anthropic.Anthropic()
    system_prompt = build_system(TEST_SEVERITY, config)

    rows = []
    suggestions: dict[str, str] = {}  # chunk_id -> rewrite
    total_in = total_out = 0
    for chunk in sample:
        applicable = [
            r for r in rules
            if r.applies_to(chunk.input_level, parsed.document_type, chunk.section)
        ]
        log.info("%s [%s | %s | %s]: %d applicable rules",
                 chunk.chunk_id, chunk.input_level, chunk.tab_title,
                 chunk.section, len(applicable))
        breached: list = []
        for rule in applicable:
            result = run_check(client, model, TEST_SEVERITY, rule, chunk, config)
            total_in += result.input_tokens
            total_out += result.output_tokens
            if result.flag in ("r", "a"):
                breached.append(rule)
            log.info("  %s -> %s", rule.rule_id, result.flag)
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
                "severity": TEST_SEVERITY,
                "flag": result.flag,
                "raw_response": result.raw_response,
                "system_prompt": system_prompt,
                "user_prompt": build_user_text(rule, chunk),
                "model": model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            })

        # Second loop: feed the breached rules back and ask for a rewrite
        # that fixes only those breaches. Figures are not rewritten.
        if breached and chunk.input_level != "figure":
            before, after = neighbour_context(sample, chunk)
            rewrite = run_rewrite(client, rewrite_model, breached, chunk, config,
                                  context_before=before, context_after=after)
            total_in += rewrite.input_tokens
            total_out += rewrite.output_tokens
            suggestions[chunk.chunk_id] = rewrite.suggestion
            log.info("  rewrite (%d breached rules) -> %d chars",
                     len(breached), len(rewrite.suggestion))

    for row in rows:
        row["suggestion"] = suggestions.get(row["chunk_id"], "")

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / "test_run.csv"
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
    (DATA_DIR / "test_run.json").write_text(
        json.dumps(
            {
                "doc_id": parsed.doc_id,
                "title": parsed.title,
                "document_type": parsed.document_type,
                "severity": TEST_SEVERITY,
                "model": model,
                "chunks": list(by_chunk.values()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    update_flag_history(parsed.title, rows)
    log.info("Flag history updated -> %s", FLAG_HISTORY_CSV.name)

    flags = [r["flag"] for r in rows]
    log.info("%d checks -> %s (+ test_run.json)", len(rows), out_path)
    log.info("Flags: r=%d a=%d g=%d invalid=%d",
             flags.count("r"), flags.count("a"), flags.count("g"),
             flags.count("invalid"))
    log.info("Tokens: %d in / %d out (~$%.4f)",
             total_in, total_out, estimate_cost(total_in, total_out))

    post_run_checks(parsed, rows, suggestions)
    log.info("Full log: %s", log_path)


if __name__ == "__main__":
    main()
