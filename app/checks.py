"""Preflight and post-run checks. Run before/after every pipeline script
so problems are caught loudly instead of producing silently-wrong output.

Each check logs PASS / WARN / FAIL; preflight() returns False if any
check FAILed (callers should abort).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("report_checker.checks")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_STEPS = ("rag report", "overused words", "suggested improvement", "story flag")


@dataclass
class Check:
    name: str
    level: str   # PASS | WARN | FAIL
    detail: str


def _report(results: list[Check]) -> bool:
    width = max(len(c.name) for c in results)
    for c in results:
        line = f"  [{c.level:4}] {c.name:<{width}}  {c.detail}"
        if c.level == "FAIL":
            log.error(line)
        elif c.level == "WARN":
            log.warning(line)
        else:
            log.info(line)
    failed = [c for c in results if c.level == "FAIL"]
    if failed:
        log.error("Preflight FAILED (%d checks) - aborting.", len(failed))
    return not failed


def preflight(config=None, rules=None, require_api_key: bool = True,
              doc_id: str | None = None) -> bool:
    """Environment + config sanity checks. Pass the already-loaded config and
    rules to validate them; pass doc_id to verify document access."""
    results: list[Check] = []

    # 1. service account key
    key_file = PROJECT_ROOT / os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
    if key_file.exists():
        try:
            email = json.loads(key_file.read_text(encoding="utf-8")).get("client_email", "")
            results.append(Check("service account", "PASS", email))
        except Exception:  # noqa: BLE001
            results.append(Check("service account", "FAIL", f"{key_file.name} is not valid JSON"))
    else:
        results.append(Check("service account", "FAIL", f"{key_file} missing"))

    # 2. Anthropic API key
    if os.environ.get("ANTHROPIC_API_KEY"):
        results.append(Check("anthropic api key", "PASS", "present"))
    else:
        results.append(Check(
            "anthropic api key", "FAIL" if require_api_key else "WARN",
            "ANTHROPIC_API_KEY not set" + ("" if require_api_key else " - AI steps will be skipped"),
        ))

    # 3. config completeness
    if config is not None:
        missing_steps = [s for s in REQUIRED_STEPS if not config.model_for(s)]
        results.append(Check(
            "step models", "FAIL" if missing_steps else "PASS",
            f"missing: {missing_steps}" if missing_steps
            else ", ".join(f"{s}={config.model_for(s)}" for s in REQUIRED_STEPS),
        ))
        severity = config.check_severity
        has_instruction = severity in config.severity_instructions
        results.append(Check(
            "severity", "FAIL" if not severity else ("WARN" if not has_instruction else "PASS"),
            "check_severity empty in config" if not severity else (
                f"'{severity}' has no flag_instruction (code fallback used)"
                if not has_instruction else
                f"'{severity}' with instruction from sheet"),
        ))
        results.append(Check(
            "batching / cache", "PASS",
            f"batching={'yes' if config.batching else 'no'}, "
            f"cache={'yes' if config.cache else 'no'}",
        ))
        for name, values in (("document_types", config.document_types),
                             ("sections", config.sections)):
            results.append(Check(
                name, "FAIL" if not values else "PASS",
                "empty in config" if not values else ", ".join(values),
            ))

    # 4. rules
    if rules is not None:
        if not rules:
            results.append(Check("style rules", "FAIL", "no active rules (include=yes)"))
        else:
            no_example = sum(1 for r in rules if not r.example)
            detail = f"{len(rules)} active"
            if no_example:
                detail += f"; {no_example} without an Example:"
            levels = {r.input_level for r in rules}
            results.append(Check("style rules", "PASS", f"{detail}; levels: {sorted(levels)}"))

    # 5. document access
    if doc_id:
        try:
            from app.auth import docs_service
            doc = docs_service().documents().get(documentId=doc_id).execute()
            results.append(Check("document access", "PASS", repr(doc.get("title", ""))))
        except Exception as exc:  # noqa: BLE001
            results.append(Check("document access", "FAIL", str(exc)[:140]))

    log.info("Preflight checks:")
    return _report(results)


def post_run_checks(parsed, rows: list[dict], suggestions: dict[str, str]) -> bool:
    """Sanity checks on a finished check run."""
    results: list[Check] = []

    results.append(Check(
        "document_type", "WARN" if parsed.document_type == "unknown" else "PASS",
        parsed.document_type,
    ))

    invalid = [r["chunk_id"] for r in rows if r["flag"] not in ("r", "a", "g")]
    results.append(Check(
        "flag validity", "FAIL" if invalid else "PASS",
        f"invalid responses on: {invalid}" if invalid else f"{len(rows)} checks all r/a/g",
    ))

    # every breached non-figure chunk should have a suggestion
    breached_chunks = {
        r["chunk_id"] for r in rows
        if r["flag"] in ("r", "a") and r["input_level"] != "figure"
    }
    missing = sorted(c for c in breached_chunks if not suggestions.get(c))
    results.append(Check(
        "suggestions", "WARN" if missing else "PASS",
        f"breached chunks without a rewrite: {missing}" if missing
        else f"{len(suggestions)} rewrites for {len(breached_chunks)} breached chunks",
    ))

    empty_chunks = [c.chunk_id for c in parsed.chunks
                    if c.input_level != "figure" and not c.text.strip()]
    results.append(Check(
        "empty chunks", "WARN" if empty_chunks else "PASS",
        f"{len(empty_chunks)} empty: {empty_chunks[:5]}" if empty_chunks else "none",
    ))

    log.info("Post-run checks:")
    return _report(results)
