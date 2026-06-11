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

from app.check_engine import build_system, build_user_text, estimate_cost, run_check
from app.docs_parser import Chunk, parse_document
from app.styleguide import load_config, load_rules

TEST_DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent / "data"
TEST_SEVERITY = "high"  # forced in test mode

load_dotenv()


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
    model = config.claude_model
    if not model:
        raise RuntimeError("claude_model_selection is empty in the config tab.")
    rules = load_rules()
    print(f"Model: {model} | severity: {TEST_SEVERITY} (forced) | {len(rules)} active rules")

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
    total_in = total_out = 0
    for chunk in sample:
        applicable = [
            r for r in rules
            if r.applies_to(chunk.input_level, parsed.document_type, chunk.section)
        ]
        print(f"\n{chunk.chunk_id} [{chunk.input_level} | {chunk.tab_title} | "
              f"{chunk.section}]: {len(applicable)} applicable rules")
        for rule in applicable:
            result = run_check(client, model, TEST_SEVERITY, rule, chunk, config)
            total_in += result.input_tokens
            total_out += result.output_tokens
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
            "text": chunk.text,
            "image": (chunk.figures[0].image_path if chunk.figures else None),
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

    flags = [r["flag"] for r in rows]
    print(f"\n{len(rows)} checks -> {out_path} (+ test_run.json)")
    print(f"Flags: r={flags.count('r')} a={flags.count('a')} g={flags.count('g')} "
          f"invalid={flags.count('invalid')}")
    print(f"Tokens: {total_in} in / {total_out} out "
          f"(~${estimate_cost(total_in, total_out):.4f})")


if __name__ == "__main__":
    main()
