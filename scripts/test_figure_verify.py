"""Confirm the verification pass now passes the figure footer to the model."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import anthropic
from dotenv import load_dotenv

from app.check_engine import build_verify_params, run_verification
from app.docs_parser import parse_document
from app.styleguide import load_config, load_rules

load_dotenv()
config = load_config()
rules = load_rules()
footer_rule = next(r for r in rules if r.figure_type == "footer")
print("rule:", footer_rule.text[:80])

parsed = parse_document(config.report_doc_id, allowed_types=config.document_types,
                        image_dir=Path("data/images"))
fig = next(c for c in parsed.chunks
           if c.input_level == "figure" and c.figures and c.figures[0].image_path)
print("figure:", fig.chunk_id)

params = build_verify_params(config.model_for("verification"), footer_rule, fig, config)
content_types = [b.get("type") for b in params["messages"][0]["content"]]
print("content blocks sent to verifier:", content_types)  # expect image + text

v = run_verification(anthropic.Anthropic(), config.model_for("verification"),
                     footer_rule, fig, config)
print("verdict:", v.verdict)
print("note:", v.note)
