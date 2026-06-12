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
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.docs_parser import Chunk
from app.styleguide import Rule, StyleGuideConfig

VALID_FLAGS = {"r", "a", "g"}

PRICE_PER_MTOK = {"input": 1.0, "output": 5.0}  # Haiku 4.5

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
        or SEVERITY_INSTRUCTIONS[severity]
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


def run_check(
    client: anthropic.Anthropic,
    model: str,
    severity: str,
    rule: Rule,
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
) -> CheckResult:
    content: list[dict] = [{"type": "text", "text": build_user_text(rule, chunk)}]
    if chunk.input_level == "figure":
        figure = chunk.figures[0]
        if figure.image_path and Path(figure.image_path).exists():
            content.insert(0, _image_block(figure.image_path))

    input_tokens = output_tokens = 0
    raw = ""
    # one retry if the model strays from the single-letter format
    for nudge in ("", "Reply with one letter only: r, a or g."):
        messages = [{"role": "user", "content": content}]
        if nudge:
            messages.append({"role": "assistant", "content": raw or "?"})
            messages.append({"role": "user", "content": nudge})
        response = client.messages.create(
            model=model,
            max_tokens=4,
            system=[{
                "type": "text",
                "text": build_system(severity, config),
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        flag = raw.lower().strip(".")
        if flag in VALID_FLAGS:
            return CheckResult(flag, raw, input_tokens, output_tokens)
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
    role = (config.role_context if config else "") or ROLE_CONTEXT
    rewrite_text = _override(config, "rewrite_instruction", REWRITE_INSTRUCTION)
    context_note = _override(config, "rewrite_context_note", REWRITE_CONTEXT_NOTE)
    system_text = f"{role}\n\n{rewrite_text}\n\n{context_note}"
    # Optional pages of "perfectly written" T&E text from config - sits in
    # the cached system prefix, so repeat calls read it at ~10% token cost.
    exemplar = _override(config, "style_exemplar", "")
    if exemplar:
        system_text += ("\n\nThe following passages show the exact style "
                        f"your rewrites must match:\n{exemplar}")
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": build_rewrite_user_text(rules, chunk, context_before, context_after),
        }],
    )
    suggestion = "".join(b.text for b in response.content if b.type == "text").strip()
    return RewriteResult(
        suggestion=suggestion,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


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
        max_tokens=1500,
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
        max_tokens=1000,
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
