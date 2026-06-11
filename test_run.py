"""Test mode: run the style rules over a small sample of chunks.

Sample = one figure + one paragraph each from the executive summary, the
main text (a numbered chapter) and the recommendations tab. Severity is
forced to HIGH. One API call per (chunk, rule); results land in
data/test_run.csv with the exact prompts that were sent.

Usage:
    python test_run.py [doc_id]
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from app.check_engine import (
    build_system, build_user_text, estimate_cost, run_check, run_rewrite,
)
from app.docs_parser import Chunk, parse_document
from app.styleguide import load_config, load_rules

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"
FLAG_HISTORY_CSV = Path(__file__).resolve().parent / "flag_history.csv"
TEST_SEVERITY = "high"  # forced in test mode

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


def pick_sample(chunks: list[Chunk]) -> list[Chunk]:
    """One figure + one paragraph from exec summary / main text / recommendations."""
    def first(pred) -> Chunk | None:
        return next((c for c in chunks if pred(c)), None)

    sample = [
        first(lambda c: c.input_level == "figure"
              and c.figures and c.figures[0].image_path),
        first(lambda c: c.input_level == "paragraph"
              and c.section == "executive summary" and len(c.text) > 200),
        first(lambda c: c.input_level == "paragraph"
              and c.section == "main text"
              and not c.tab_title.lower().startswith("6")
              and len(c.text) > 200),
        first(lambda c: c.input_level == "paragraph"
              and c.tab_title.lower().startswith("6") and len(c.text) > 50),
    ]
    missing = [i for i, c in enumerate(sample) if c is None]
    if missing:
        raise RuntimeError(f"Could not find sample chunk(s) {missing} in the document.")
    return sample


def main() -> None:
    doc_id = sys.argv[1] if len(sys.argv) > 1 else TEST_DOC_ID

    config = load_config()
    model = config.model_for("rag report")
    rewrite_model = config.model_for("suggested improvement")
    if not model:
        raise RuntimeError("claude_model_selection is empty in the config tab.")
    rules = load_rules()
    print(f"Models: checks={model} rewrites={rewrite_model} | "
          f"severity: {TEST_SEVERITY} (forced) | {len(rules)} active rules")

    parsed = parse_document(
        doc_id,
        allowed_types=config.document_types,
        image_dir=DATA_DIR / "images",
    )
    sample = pick_sample(parsed.chunks)
    print(f"Document: {parsed.title!r} ({parsed.document_type}), "
          f"{len(parsed.chunks)} chunks; sampled {len(sample)}")

    client = anthropic.Anthropic()
    system_prompt = build_system(TEST_SEVERITY, config)
    print(f"role_context from sheet: {'yes' if config.role_context else 'NO - using fallback'}; "
          f"severity instructions from sheet: {sorted(config.severity_instructions)}")

    rows = []
    suggestions: dict[str, str] = {}  # chunk_id -> rewrite
    total_in = total_out = 0
    for chunk in sample:
        applicable = [
            r for r in rules
            if r.applies_to(chunk.input_level, parsed.document_type, chunk.section)
        ]
        print(f"\n{chunk.chunk_id} [{chunk.input_level} | {chunk.tab_title} | "
              f"{chunk.section}]: {len(applicable)} applicable rules")
        breached: list = []
        for rule in applicable:
            result = run_check(client, model, TEST_SEVERITY, rule, chunk, config)
            total_in += result.input_tokens
            total_out += result.output_tokens
            if result.flag in ("r", "a"):
                breached.append(rule)
            print(f"  {rule.rule_id} -> {result.flag}")
            rows.append({
                "chunk_id": chunk.chunk_id,
                "tab": chunk.tab_title,
                "section": chunk.section,
                "input_level": chunk.input_level,
                "chunk_text": chunk.text,
                "rule_id": rule.rule_id,
                "category": rule.category,
                "rule": rule.text,
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
            rewrite = run_rewrite(client, rewrite_model, breached, chunk, config)
            total_in += rewrite.input_tokens
            total_out += rewrite.output_tokens
            suggestions[chunk.chunk_id] = rewrite.suggestion
            print(f"  rewrite ({len(breached)} breached rules) -> "
                  f"{len(rewrite.suggestion)} chars")

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
    print(f"\nFlag history updated -> {FLAG_HISTORY_CSV.name}")

    flags = [r["flag"] for r in rows]
    print(f"{len(rows)} checks -> {out_path} (+ test_run.json)")
    print(f"Flags: r={flags.count('r')} a={flags.count('a')} g={flags.count('g')} "
          f"invalid={flags.count('invalid')}")
    print(f"Tokens: {total_in} in / {total_out} out "
          f"(~${estimate_cost(total_in, total_out):.4f})")


if __name__ == "__main__":
    main()
