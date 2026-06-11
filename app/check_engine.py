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

REWRITE_FIGURE_INSTRUCTION = (
    "You will receive a figure from the draft (as an image) and the list "
    "of style rules it breaches. A figure cannot be rewritten as text, so "
    "instead give the author short, concrete instructions to fix every "
    "listed breach - at most three bullet points, nothing else."
)


def build_system(severity: str, config: StyleGuideConfig | None = None) -> str:
    role = (config.role_context if config else "") or ROLE_CONTEXT
    severity_text = (
        (config.severity_instructions.get(severity, "") if config else "")
        or SEVERITY_INSTRUCTIONS[severity]
    )
    return "\n\n".join([role, severity_text, FLAG_INSTRUCTION])


def build_user_text(rule: Rule, chunk: Chunk) -> str:
    intro = (
        f"Rule ({rule.category}):\n{rule.text}\n\n"
        f"Extract ({chunk.input_level} from the {chunk.section} of a "
        f"{chunk.document_type}):"
    )
    if chunk.input_level == "figure":
        caption = chunk.text or "(no caption or alt text provided)"
        return f"{intro}\nThe figure is attached as an image. Caption/alt text: {caption}"
    return f"{intro}\n{chunk.text}"


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


def build_rewrite_user_text(rules: list[Rule], chunk: Chunk) -> str:
    listed = "\n".join(f"{i}. ({r.category}) {r.text}" for i, r in enumerate(rules, 1))
    label = (
        f"Extract ({chunk.input_level} from the {chunk.section} of a "
        f"{chunk.document_type}):"
    )
    if chunk.input_level == "figure":
        caption = chunk.text or "(no caption or alt text provided)"
        body = f"{label}\nThe figure is attached as an image. Caption/alt text: {caption}"
    else:
        body = f"{label}\n{chunk.text}"
    return f"Rules breached:\n{listed}\n\n{body}"


def run_rewrite(
    client: anthropic.Anthropic,
    model: str,
    rules: list[Rule],
    chunk: Chunk,
    config: StyleGuideConfig | None = None,
) -> RewriteResult:
    """Second loop: rewrite the chunk fixing only the breached rules."""
    role = (config.role_context if config else "") or ROLE_CONTEXT
    instruction = (
        REWRITE_FIGURE_INSTRUCTION if chunk.input_level == "figure"
        else REWRITE_INSTRUCTION
    )
    content: list[dict] = [{"type": "text", "text": build_rewrite_user_text(rules, chunk)}]
    if chunk.input_level == "figure":
        figure = chunk.figures[0]
        if figure.image_path and Path(figure.image_path).exists():
            content.insert(0, _image_block(figure.image_path))

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{
            "type": "text",
            "text": f"{role}\n\n{instruction}",
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": content}],
    )
    suggestion = "".join(b.text for b in response.content if b.type == "text").strip()
    return RewriteResult(
        suggestion=suggestion,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * PRICE_PER_MTOK["input"]
        + output_tokens * PRICE_PER_MTOK["output"]
    ) / 1_000_000
