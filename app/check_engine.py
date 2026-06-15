"""Run style rules against document chunks via the Claude API.

One API call per (chunk, rule). The response is constrained to a single
flag letter to keep output tokens minimal:
    r = red (clear violation), a = amber (borderline), g = green (complies)

The role/context and per-severity instructions come from the
master_report_checker config tab (`role_context`, `flag_instruction`
paired with `check_severity`); the texts below are fallbacks for when a
config value is missing.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.docs_parser import Chunk
from app.styleguide import Rule, StyleGuideConfig

VALID_FLAGS = {"r", "a", "g"}

PRICE_PER_MTOK = {"input": 1.0, "output": 5.0}  # Haiku 4.5 (default fallback)

# $ per million tokens, matched by model-id prefix.
MODEL_PRICES = {
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
}


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    price = next((p for prefix, p in MODEL_PRICES.items()
                  if (model or "").startswith(prefix)), PRICE_PER_MTOK)
    return (input_tokens * price["input"] + output_tokens * price["output"]) / 1_000_000

# ---- prompt building blocks (destined for the config tab) ----------------

ROLE_CONTEXT = (
    "You are a meticulous style-guide checker for Transport & Environment "
    "(T&E), a European clean-transport campaign organisation. You review "
    "extracts from draft publications before they are published. You judge "
    "one extract against one style rule at a time. Judge only the rule you "
    "are given - ignore any other problem the extract may have."
)

SEVERITY_INSTRUCTIONS = {
    "low": (
        "Apply the spirit of the rule generously. Flag r only for blatant, "
        "serious violations. When in doubt, answer g."
    ),
    "mid": (
        "Apply the rule with balanced judgement. Flag r for clear "
        "violations and a for genuinely borderline cases."
    ),
    "high": (
        "Apply the letter of the rule strictly. Flag every violation, "
        "however minor. When in doubt between g and a, answer a."
    ),
}

FLAG_INSTRUCTION = (
    "Answer with exactly one lowercase letter and nothing else:\n"
    "r = the extract clearly violates the rule (red flag)\n"
    "a = borderline or possible violation (amber flag)\n"
    "g = the extract complies with the rule (green)"
)

REWRITE_CONTEXT_NOTE = (
    "You may be shown the paragraphs immediately before and after the "
    "extract, and its section title, as context. Use them only to keep the "
    "rewrite consistent with the surrounding flow, tense and terminology. "
    "Rewrite ONLY the extract."
)

STORY_FLAG_INSTRUCTION = (
    "You will receive the complete sequence of section and sub-section "
    "titles from a draft publication, in order. Judge whether reading them "
    "top to bottom tells a convincing story: a clear narrative arc from "
    "problem to evidence to conclusion, titles that convey findings rather "
    "than topics, logical ordering, and no obvious gaps or repetition. "
    "Answer with a flag - r (the headings do not tell a story), a (partly "
    "convincing, clear weaknesses) or g (a convincing story) - and a brief "
    "explanation of at most three sentences naming the strongest and "
    "weakest points."
)

STORY_FLAG_SCHEMA = {
    "type": "object",
    "properties": {
        "flag": {"type": "string", "enum": ["r", "a", "g"]},
        "explanation": {"type": "string"},
    },
    "required": ["flag", "explanation"],
    "additionalProperties": False,
}

REWRITE_INSTRUCTION = (
    "You will receive an extract from the draft and the list of style "
    "rules it breaches. Rewrite the extract so that every listed breach "
    "is fixed. Make NO changes beyond what is needed to fix the listed "
    "breaches: keep the meaning, facts, figures, names, terminology and "
    "tone exactly as they are. Never introduce a claim, opinion or fact "
    "that is not already in the extract - if fixing a breach requires an "
    "extra sentence, build it only from what the extract already says. "
    "Return only the rewritten extract - no preamble, no explanation, no "
    "quotation marks around it."
)

WORD_FLAG_INSTRUCTION = (
    "You will receive the most frequent words from a draft publication "
    "with their counts (small connecting words and the document's core "
    "topic vocabulary have already been removed). Identify the words "
    "whose overuse signals a writing-style problem: filler adverbs, "
    "hedges, intensifiers, vague qualifiers and overworked transitions "
    "or emphasis words (for example: while, also, strong, significant, "
    "crucial, only, key). Do NOT flag words that simply reflect the "
    "document's subject matter (for example: value, materials, demand, "
    "billion). For each flagged word give a reason of at most six words."
)

WORD_FLAG_SCHEMA = {
    "type": "object",
    "properties": {
        "flagged": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["word", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["flagged"],
    "additionalProperties": False,
}


def _override(config: StyleGuideConfig | None, column: str, fallback: str) -> str:
    return (config.prompt_override(column) if config else "") or fallback


def build_system(severity: str, config: StyleGuideConfig | None = None) -> str:
    role = (config.role_context if config else "") or ROLE_CONTEXT
    severity_text = (
        (config.severity_instructions.get(severity, "") if config else "")
        or SEVERITY_INSTRUCTIONS.get(severity, SEVERITY_INSTRUCTIONS["mid"])
    )
    flag_text = _override(config, "flag_letters_instruction", FLAG_INSTRUCTION)
    return "\n\n".join([role, severity_text, flag_text])


FORMATTING_NOTE = (
    "Formatting in the extract is marked inline: **bold**, *italic*, "
    "<u>underlined</u>, [link text](url)."
)


def _rule_block(rule: Rule) -> str:
    block = f"Rule:\n{rule.text}"
    if rule.example:
        block += f"\nExample: {rule.example}"
    return block


def _chunk_body(chunk: Chunk) -> str:
    """The extract as fed to the model - formatted version when it carries
    formatting (some rules depend on bold/underline/links)."""
    formatted = chunk.formatted_text or chunk.text
    if formatted != chunk.text:
        return f"{FORMATTING_NOTE}\n{formatted}"
    return chunk.text


def build_user_text(rule: Rule, chunk: Chunk) -> str:
    intro = (
        f"{_rule_block(rule)}\n\n"
        f"Extract ({chunk.input_level} from the {chunk.section} of a "
        f"{chunk.document_type}):"
    )
    if chunk.input_level == "figure":
        caption = chunk.text or "(no caption or alt text provided)"
        return f"{intro}\nThe figure is attached as an image. Caption/alt text: {caption}"
    return f"{intro}\n{_chunk_body(chunk)}"


# ---- API calls ------------------------------------------------------------

@dataclass
class CheckResult:
    flag: str            # r | a | g | invalid
    raw_response: str
    input_tokens: int
    output_tokens: int


def _image_block(image_path: str) -> dict:
    data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _pil_image_block(image) -> dict:
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = base64.standard_b64encode(buffer.getvalue()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }


def _system_blocks(text: str, config: StyleGuideConfig | None) -> list[dict]:
    """System prompt block; cache_control only when config cache = yes."""
    block: dict = {"type": "text", "text": text}
    if config is not None and config.cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def figure_parts_for(image_path: str) -> dict:
    """OCR part geometry for a figure (cached per path in figure_parts)."""
    try:
        from app.figure_parts import extract_parts_path
        return extract_parts_path(image_path)
    except Exception as exc:  # noqa: BLE001 — OCR is best-effort
        logging.getLogger(__name__).warning(
            "figure part OCR failed for %s (%s); using whole image",
            image_path, exc)
        return {}


def _figure_content(rule: Rule, chunk: Chunk) -> list[dict]:
    """Content blocks for a figure check. When the rule names a
    figure_type (header/sub_header/footer), only that part of the image
    is sent; whole_image or blank sends the full figure."""
    figure = chunk.figures[0] if chunk.figures else None
    image_path = figure.image_path if figure else None
    if not image_path or not Path(image_path).exists():
        return []

    part = (rule.figure_type or "whole_image").lower()
    if part in ("", "whole_image"):
        return [_image_block(image_path)]

    from app.figure_parts import FIGURE_TYPE_TO_PART, crop_part
    parts = figure_parts_for(image_path)
    cropped = crop_part(image_path, part, parts) if parts else None
    if cropped is None:
        part_name = FIGURE_TYPE_TO_PART.get(part, part)
        return [{"type": "text",
                 "text": f"(No {part_name} was detected on this figure.)"}]
    blocks: list[dict] = [_pil_image_block(cropped)]
    text = parts.get(FIGURE_TYPE_TO_PART.get(part, part), {}).get("text", "")
    if text:
        blocks.append({"type": "text",
                       "text": f"OCR text of this part: {text}"})
    return blocks


def build_check_params(
    model: str,
    severity: str,
    rule: Rule,
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
) -> dict:
    """messages.create kwargs for one (chunk, rule) check - shared by the
    serial path and the Batches API path."""
    content: list[dict] = []
    if chunk.input_level == "figure":
        content.extend(_figure_content(rule, chunk))
    content.append({"type": "text", "text": build_user_text(rule, chunk)})
    max_tokens = config.max_tokens_for("rag report", 4, 4) if config else 4
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": _system_blocks(build_system(severity, config), config),
        "messages": [{"role": "user", "content": content}],
    }


def _parse_flag(response) -> CheckResult:
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    flag = raw.lower().strip(".")
    return CheckResult(
        flag if flag in VALID_FLAGS else "invalid", raw,
        response.usage.input_tokens, response.usage.output_tokens,
    )


def run_check(
    client: anthropic.Anthropic,
    model: str,
    severity: str,
    rule: Rule,
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
) -> CheckResult:
    params = build_check_params(model, severity, rule, chunk, config)
    input_tokens = output_tokens = 0
    raw = ""
    # one retry if the model strays from the single-letter format
    for nudge in ("", "Reply with one letter only: r, a or g."):
        messages = list(params["messages"])
        if nudge:
            messages.append({"role": "assistant", "content": raw or "?"})
            messages.append({"role": "user", "content": nudge})
        response = client.messages.create(**{**params, "messages": messages})
        result = _parse_flag(response)
        raw = result.raw_response
        input_tokens += result.input_tokens
        output_tokens += result.output_tokens
        if result.flag != "invalid":
            return CheckResult(result.flag, raw, input_tokens, output_tokens)
    return CheckResult("invalid", raw, input_tokens, output_tokens)


@dataclass
class RewriteResult:
    suggestion: str
    input_tokens: int
    output_tokens: int


def build_rewrite_user_text(
    rules: list[Rule],
    chunk: Chunk,
    context_before: str = "",
    context_after: str = "",
) -> str:
    listed = "\n".join(f"{i}. {r.text}" for i, r in enumerate(rules, 1))
    parts = [f"Rules breached:\n{listed}"]

    section_title = chunk.heading_path[-1] if chunk.heading_path else chunk.tab_title
    parts.append(f'Section: "{section_title}"')

    # Surrounding paragraphs so the rewrite keeps flow, tense and
    # terminology consistent - explicitly out of scope for the rewrite.
    if context_before:
        parts.append("Paragraph BEFORE the extract (context only - do NOT "
                     f"rewrite it):\n{context_before}")
    label = (
        f"THE EXTRACT TO REWRITE ({chunk.input_level} from the "
        f"{chunk.section} of a {chunk.document_type}):"
    )
    parts.append(f"{label}\n{_chunk_body(chunk)}")
    if context_after:
        parts.append("Paragraph AFTER the extract (context only - do NOT "
                     f"rewrite it):\n{context_after}")
    return "\n\n".join(parts)


def _rewrite_system_text(config: StyleGuideConfig | None) -> str:
    """Role + rewrite instructions + the config's additional rewrite context
    (general_rewritting_rules, best_practice_example). The whole block is
    identical across calls, so with cache = yes it forms the cached prefix
    and repeat calls read it at ~10% token cost."""
    role = (config.role_context if config else "") or ROLE_CONTEXT
    rewrite_text = _override(config, "rewrite_instruction", REWRITE_INSTRUCTION)
    context_note = _override(config, "rewrite_context_note", REWRITE_CONTEXT_NOTE)
    parts = [role, rewrite_text, context_note]

    general = _override(config, "general_rewritting_rules", "")
    if general and general.lower() != "placeholder":
        parts.append(f"General rewriting rules to always follow:\n{general}")
    exemplar = _override(config, "best_practice_example", "")
    if exemplar and exemplar.lower() != "placeholder":
        parts.append("The following passages show the exact style your "
                     f"rewrites must match:\n{exemplar}")
    return "\n\n".join(parts)


def build_rewrite_params(
    model: str,
    rules: list[Rule],
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
    context_before: str = "",
    context_after: str = "",
) -> dict:
    max_tokens = (config.max_tokens_for("suggested improvement", 2000, 500)
                  if config else 2000)
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": _system_blocks(_rewrite_system_text(config), config),
        "messages": [{
            "role": "user",
            "content": build_rewrite_user_text(rules, chunk, context_before, context_after),
        }],
    }


def run_rewrite(
    client: anthropic.Anthropic,
    model: str,
    rules: list[Rule],
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
    context_before: str = "",
    context_after: str = "",
) -> RewriteResult:
    """Second loop: rewrite the chunk fixing only the breached rules.

    Figures are not rewritten - the caller skips figure chunks.
    """
    response = client.messages.create(**build_rewrite_params(
        model, rules, chunk, config, context_before, context_after))
    suggestion = "".join(b.text for b in response.content if b.type == "text").strip()
    return RewriteResult(
        suggestion=suggestion,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# ---- Message Batches API (config batching = yes): 50% token cost ----------

def run_batch(
    client: anthropic.Anthropic,
    requests_params: dict[str, dict],
    poll_seconds: int = 15,
    timeout_seconds: int = 3600,
) -> dict[str, object]:
    """Submit {custom_id: messages.create kwargs} as one batch, poll until
    it ends, and return {custom_id: Message | None}."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    log = logging.getLogger(__name__)
    batch = client.messages.batches.create(requests=[
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params))
        for cid, params in requests_params.items()
    ])
    log.info("batch %s submitted (%d requests)", batch.id, len(requests_params))

    waited = 0
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if waited >= timeout_seconds:
            raise TimeoutError(f"batch {batch.id} still {batch.processing_status} "
                               f"after {timeout_seconds}s")
        log.info("  batch %s: %d processing...", batch.id,
                 batch.request_counts.processing)
        time.sleep(poll_seconds)
        waited += poll_seconds

    log.info("batch %s ended: %d succeeded, %d errored", batch.id,
             batch.request_counts.succeeded, batch.request_counts.errored)
    messages: dict[str, object] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            messages[result.custom_id] = result.result.message
        else:
            log.warning("  batch item %s: %s", result.custom_id, result.result.type)
            messages[result.custom_id] = None
    return messages


def parse_check_message(message) -> CheckResult:
    """Batch-path equivalent of run_check's parsing (no retry possible)."""
    if message is None:
        return CheckResult("invalid", "(batch item failed)", 0, 0)
    return _parse_flag(message)


def parse_rewrite_message(message) -> RewriteResult:
    if message is None:
        return RewriteResult("", 0, 0)
    suggestion = "".join(b.text for b in message.content if b.type == "text").strip()
    return RewriteResult(suggestion, message.usage.input_tokens,
                         message.usage.output_tokens)


# ---- verification pass: independent confirm/refute + offending quote -----

VERIFY_INSTRUCTION = (
    "You are sanity-checking a style flag raised on an extract, to remove "
    "only the obvious false positives. Default to keeping the flag. Mark "
    "'refuted' ONLY when the extract clearly does not breach the rule at "
    "all; if it genuinely or even arguably breaches the rule, mark "
    "'confirmed'. When in doubt, confirm. When confirming, copy the exact "
    "verbatim span from the extract that breaches the rule - word for word, "
    "no paraphrase. Respond with: verdict 'confirmed' or 'refuted'; quote "
    "(the verbatim offending text, or empty when refuted); and a note of at "
    "most twelve words explaining the decision."
)

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["confirmed", "refuted"]},
        "quote": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["verdict", "quote", "note"],
    "additionalProperties": False,
}


@dataclass
class VerifyResult:
    verdict: str
    quote: str
    note: str
    input_tokens: int = 0
    output_tokens: int = 0


def build_verify_params(model: str, rule: Rule, chunk: Chunk,
                        config: StyleGuideConfig | None = None) -> dict:
    role = (config.role_context if config else "") or ROLE_CONTEXT
    instruction = _override(config, "verification_instruction", VERIFY_INSTRUCTION)
    # figures: send the same cropped image part + OCR text the flag check
    # saw, so the verifier actually inspects the footer/header/etc.
    content: list[dict] = []
    if chunk.input_level == "figure":
        content.extend(_figure_content(rule, chunk))
        content.append({"type": "text",
                        "text": f"Rule:\n{rule.text}\n\nThe image above (the "
                                "relevant part of the figure) is the extract "
                                "being checked."})
    else:
        content.append({"type": "text",
                        "text": f"Rule:\n{rule.text}\n\nExtract:\n{_chunk_body(chunk)}"})
    return {
        "model": model,
        "max_tokens": (config.max_tokens_for("verification", 400, 200)
                       if config else 400),
        "system": _system_blocks(f"{role}\n\n{instruction}", config),
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": VERIFY_SCHEMA}},
    }


def _parse_verify(response) -> VerifyResult:
    import json as _json
    text = next(b.text for b in response.content if b.type == "text")
    data = _json.loads(text)
    return VerifyResult(
        data.get("verdict", "confirmed"), data.get("quote", ""),
        data.get("note", ""), response.usage.input_tokens,
        response.usage.output_tokens,
    )


def run_verification(client: anthropic.Anthropic, model: str, rule: Rule,
                     chunk: Chunk, config: StyleGuideConfig | None = None) -> VerifyResult:
    return _parse_verify(client.messages.create(
        **build_verify_params(model, rule, chunk, config)))


def parse_verify_message(message) -> VerifyResult:
    if message is None:
        return VerifyResult("confirmed", "", "(verification unavailable)")
    return _parse_verify(message)


MESSAGE_FLAG_INSTRUCTION = (
    "You will receive one section or sub-section title from a draft "
    "publication. Judge whether the title itself conveys a clear message or "
    "conclusion, not just a topic label. Answer with exactly one lowercase "
    "letter and nothing else: r (no message - just a topic), a (partly), "
    "g (a clear message)."
)


def run_message_flag(client: anthropic.Anthropic, model: str, title: str,
                     config: StyleGuideConfig | None = None) -> tuple[str, int, int]:
    """One r/a/g flag for whether a single heading conveys a clear message."""
    instruction = _override(config, "message_flag_instruction", MESSAGE_FLAG_INSTRUCTION)
    response = client.messages.create(
        model=model,
        max_tokens=(config.max_tokens_for("message flag", 4, 4) if config else 4),
        system=instruction,
        messages=[{"role": "user", "content": f"Title: {title}"}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip().lower().strip(".")
    flag = raw if raw in VALID_FLAGS else "g"
    return flag, response.usage.input_tokens, response.usage.output_tokens


def run_word_flagging(
    client: anthropic.Anthropic,
    model: str,
    words: list[dict],
    config: StyleGuideConfig | None = None,
) -> dict[str, str]:
    """AI loop over the overused-words list: returns {word: reason} for the
    words whose overuse is a style issue (not document subject matter)."""
    import json as _json

    listing = "\n".join(f"{w['word']} ({w['count']})" for w in words)
    response = client.messages.create(
        model=model,
        max_tokens=(config.max_tokens_for("overused words", 1500, 400)
                    if config else 1500),
        system=_override(config, "word_flag_instruction", WORD_FLAG_INSTRUCTION),
        messages=[{"role": "user", "content": f"Most frequent words:\n{listing}"}],
        output_config={"format": {"type": "json_schema", "schema": WORD_FLAG_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = _json.loads(text)
    known = {w["word"] for w in words}
    return {
        item["word"].lower(): item["reason"]
        for item in data.get("flagged", [])
        if item["word"].lower() in known
    }


def run_story_flag(
    client: anthropic.Anthropic,
    model: str,
    headings: list[dict],
    config: StyleGuideConfig | None = None,
) -> dict:
    """AI layer over "What is my story?": does the heading sequence tell a
    convincing story? Returns {"flag": r|a|g, "explanation": str}."""
    import json as _json

    lines = []
    for h in headings:
        indent = "  " * min(h.get("level", 0), 3)
        lines.append(f"{indent}{h['text']}")
    listing = "\n".join(lines)

    response = client.messages.create(
        model=model,
        max_tokens=(config.max_tokens_for("story flag", 1000, 300)
                    if config else 1000),
        system=_override(config, "story_flag_instruction", STORY_FLAG_INSTRUCTION),
        messages=[{
            "role": "user",
            "content": f"Section and sub-section titles, in order:\n{listing}",
        }],
        output_config={"format": {"type": "json_schema", "schema": STORY_FLAG_SCHEMA}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return _json.loads(text)


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * PRICE_PER_MTOK["input"]
        + output_tokens * PRICE_PER_MTOK["output"]
    ) / 1_000_000
