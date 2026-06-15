"""Limited live test that prompt caching is actually working for the rewrite
step (where the long general_rewritting_rules / best_practice_example live).

Makes a few rewrite calls with an identical system prompt and prints the
cache token fields from each response's usage:
  - call 1 should WRITE the cache  (cache_creation_input_tokens > 0)
  - calls 2+ should READ the cache (cache_read_input_tokens > 0, tiny input)

Run: .venv\\Scripts\\python.exe -m scripts.test_cache
"""
from __future__ import annotations

import anthropic
from dotenv import load_dotenv

from app.check_engine import _rewrite_system_text, build_rewrite_params, cost_for
from app.docs_parser import parse_document
from app.styleguide import load_config, load_rules

# Opus cacheable-prefix minimum is 1024 tokens (Haiku is 2048).
MIN_CACHE_TOKENS = 1024


def main() -> None:
    load_dotenv()
    config = load_config()
    model = config.model_for("suggested improvement")

    sys_text = _rewrite_system_text(config)
    approx_tokens = int(len(sys_text) / 3.5)  # rough; the API usage is authoritative
    print(f"cache config        : {config.cache}")
    print(f"rewrite model       : {model}")
    print(f"general rules set   : {bool(config.prompt_override('general_rewritting_rules'))}")
    print(f"best-practice set   : {bool(config.prompt_override('best_practice_example'))}")
    print(f"rewrite system size : {len(sys_text)} chars (~{approx_tokens} tokens)")
    if not config.cache:
        print("!! config cache is not 'yes' - caching is OFF; aborting test.")
        return
    if approx_tokens < MIN_CACHE_TOKENS:
        print(f"!! system prompt looks below the ~{MIN_CACHE_TOKENS}-token minimum "
              "cacheable prefix - caching may not engage.")

    rules = [r for r in load_rules() if not r.coded][:3]
    parsed = parse_document(config.report_doc_ids[0],
                            allowed_types=config.document_types)
    paras = [c for c in parsed.chunks if c.input_level == "paragraph"][:3]
    if len(paras) < 3:
        print("!! need at least 3 paragraph chunks for the test")
        return

    client = anthropic.Anthropic()
    print("\nmaking 3 rewrite calls (identical system prompt, different extract):")
    rows = []
    for i, chunk in enumerate(paras, 1):
        params = build_rewrite_params(model, rules, chunk, config)
        u = client.messages.create(**params).usage
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        rows.append((i, u.input_tokens, write, read, u.output_tokens))
        print(f"  call {i}: input={u.input_tokens:>5}  cache_write={write:>5}  "
              f"cache_read={read:>5}  output={u.output_tokens}")

    first_write = rows[0][2]
    later_reads = [r[3] for r in rows[1:]]
    print("\nverdict:")
    if first_write > 0 and all(r > 0 for r in later_reads):
        print(f"  PASS - cache written on call 1 ({first_write} tokens) and read on "
              f"calls 2-3 ({later_reads} tokens).")
        # rough saving: later calls read the prefix at 0.1x instead of 1x input
        saved = sum(later_reads) * (cost_for(model, 1, 0) * 0.9)
        print(f"  ~cache saving across these 2 reuse calls: ${saved:.4f} "
              "(prefix billed at ~10% instead of 100%).")
    elif first_write > 0:
        print(f"  PARTIAL - cache written ({first_write}) but not read back "
              f"(reads={later_reads}). Check the system prefix is identical/stable.")
    else:
        print(f"  FAIL - no cache_creation on call 1. Caching not engaging "
              f"(prefix too short, or cache flag not applied). reads={later_reads}")


if __name__ == "__main__":
    main()
