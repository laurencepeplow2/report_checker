"""Load the master_report_checker Google Sheet (config + TE_style_rules tabs).

All keys and values are lowercased on read — the sheet is hand-edited and
case drifts. The model id keeps its original case but is whitespace-stripped.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from app.auth import drive_service, sheets_service

SHEET_NAME = "master_report_checker"
CONFIG_TAB = "config"
RULES_TAB = "TE_style_rules"

# The rules tab maps "figures" to what the parser calls a "figure" chunk.
RULE_LEVEL_TO_CHUNK_LEVEL = {
    "paragraph": "paragraph",
    "figures": "figure",
    "figure": "figure",
}


# Old step names (still used in code) -> config tag names. The tag /
# claude_model / role triplet in the config tab carries one row per AI
# step: its name, the model it runs on, and its instruction text.
STEP_TAGS = {
    "rag report": "flag_letters_instruction",
    "suggested improvement": "rewrite_instruction",
    "overused words": "word_flag_instruction",
    "story flag": "story_flag_instruction",
    "message flag": "message_flag_instruction",
    "verification": "verification_instruction",
}


@dataclass
class StyleGuideConfig:
    claude_model: str = ""
    check_severity: str = ""
    document_types: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    input_levels: list[str] = field(default_factory=list)
    role_context: str = ""
    severity_instructions: dict[str, str] = field(default_factory=dict)
    # tag -> model and tag -> instruction text, from the config triplet
    step_models: dict[str, str] = field(default_factory=dict)
    prompts: dict[str, str] = field(default_factory=dict)
    report_link: str = ""
    batching: bool = False
    cache: bool = False
    verify: bool = True   # run the independent verification pass on flags
    mode: str = ""        # run profile name, e.g. "main", "test_1"
    max_pages: int = 0    # page cap for the run; 0 = whole document
    # hard EUR ceiling for one run (config max_report_cost_eur); 0 = no cap.
    # If the pre-run estimate exceeds it, no AI calls are made; if a live run
    # reaches it, the run stops gracefully and writes what it has so far.
    max_report_cost_eur: int = 0
    # clear_UI = yes: keep only the just-run report in the UI selector (prunes
    # the index; run data on disk is left untouched).
    clear_ui: bool = False
    # number of parallel API calls for the serial (non-batching) loops; 1 =
    # serial. Higher cuts wall-clock; too high may hit Anthropic rate limits.
    concurrency: int = 8
    # document_type -> max allowed pages (config page_limit column)
    page_limits: dict[str, int] = field(default_factory=dict)
    # tag -> max output tokens for that AI step (config max_token column)
    step_max_tokens: dict[str, int] = field(default_factory=dict)
    # flag letter -> sentence word limit (sentence_word_flag/_limit columns,
    # e.g. {"a": 12, "r": 16}) for the coded sentence-length check
    sentence_word_limits: dict[str, int] = field(default_factory=dict)
    raw: dict[str, list[str]] = field(default_factory=dict)

    def model_for(self, step: str) -> str:
        tag = STEP_TAGS.get(step.lower(), step.lower())
        return self.step_models.get(tag, "") or self.claude_model

    def max_tokens_for(self, step: str, default: int, floor: int) -> int:
        """Configured output-token cap for a step. A hard floor keeps runs
        functional when the sheet value is too small for the response
        format (e.g. truncated JSON or mid-sentence rewrites)."""
        tag = STEP_TAGS.get(step.lower(), step.lower())
        configured = self.step_max_tokens.get(tag, 0)
        if not configured:
            return default
        if configured < floor:
            logging.getLogger(__name__).warning(
                "max_token %d for %s is below the working floor %d - using %d",
                configured, tag, floor, floor)
            return floor
        return configured

    def prompt_override(self, name: str) -> str:
        """Instruction text for a tag (or any config column) from the sheet.
        Returns "" when absent (callers fall back to the in-code default)."""
        name = name.lower()
        if self.prompts.get(name):
            return self.prompts[name]
        values = self.raw.get(name, [])
        return values[0].strip() if values else ""

    @property
    def report_doc_ids(self) -> list[str]:
        """All doc ids found in the report_link column - the column may
        hold several links (one per row, or several in one cell)."""
        ids = re.findall(r"/document/d/([A-Za-z0-9_-]+)", self.report_link)
        seen: set[str] = set()
        return [i for i in ids if not (i in seen or seen.add(i))]

    @property
    def report_doc_id(self) -> str:
        ids = self.report_doc_ids
        return ids[0] if ids else ""


@dataclass
class Rule:
    rule_id: str            # row-derived, e.g. "rule-002" (sheet row number)
    category: str           # no longer in the sheet; kept for output compat
    text: str               # the rule itself (sheet "rule" column)
    input_level: str        # normalised chunk level: paragraph | figure | subsection
    right: str = ""         # a correct example (sheet "right" column) - fed to AI
    wrong: str = ""         # a breaching example (sheet "wrong" column) - fed to AI
    figure_type: str = ""   # header | sub_header | footer | whole_image | ""
    coded: bool = False     # include_AI_check = coded: deterministic code check
    number_check: bool = False    # only run on paragraphs that contain a number
    hyperlink_rule: bool = False  # only run on chunks that contain a hyperlink
    rule_tag: str = ""      # 2-3 word summary of the rule (sheet rule_tag column)
    document_types: set[str] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)

    def applies_to(self, input_level: str, document_type: str, section: str) -> bool:
        return (
            self.input_level == input_level
            and document_type in self.document_types
            and section in self.sections
        )


def find_sheet_id() -> str:
    """Resolve the spreadsheet ID from env or by name via Drive search."""
    env_id = os.environ.get("MASTER_SHEET_ID")
    if env_id:
        return env_id
    resp = drive_service().files().list(
        q=f"name = '{SHEET_NAME}' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false",
        fields="files(id, name)",
    ).execute()
    files = resp.get("files", [])
    if not files:
        raise RuntimeError(
            f"Spreadsheet '{SHEET_NAME}' is not visible to the service account. "
            "Share it (Viewer is enough) or set MASTER_SHEET_ID in .env."
        )
    return files[0]["id"]


# Columns holding controlled vocab are lowercased; prompt-text columns
# keep their case as written in the sheet.
VOCAB_COLUMNS = {"input_level", "document_type", "section", "check_severity"}


def load_config(sheet_id: str | None = None) -> StyleGuideConfig:
    """Read the config tab.

    Layout (validated against the real sheet): row 1 holds variable names,
    values listed beneath each header. The `flag_instruction` column holds
    the per-severity instruction, paired row-by-row with `check_severity`
    (low/mid/high). `role_context:` (trailing colon in the sheet) holds the
    system-prompt role text.
    """
    sheet_id = sheet_id or find_sheet_id()
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{CONFIG_TAB}'!A1:AZ100"
    ).execute().get("values", [])
    if not values:
        raise RuntimeError(f"'{CONFIG_TAB}' tab is empty or missing.")

    headers = [h.strip().lower().rstrip(":") for h in values[0]]
    columns: dict[str, list[str]] = {h: [] for h in headers if h}
    for row in values[1:]:
        for idx, header in enumerate(headers):
            if header and idx < len(row) and row[idx].strip():
                value = " ".join(row[idx].split())  # collapse stray whitespace
                columns[header].append(value.lower() if header in VOCAB_COLUMNS else value)

    # The tag / claude_model / role triplet: one row per AI step with its
    # name, model, and instruction text (literal "\n" unescaped).
    step_models: dict[str, str] = {}
    prompts: dict[str, str] = {}
    step_max_tokens: dict[str, int] = {}
    if "tag" in headers:
        tag_idx = headers.index("tag")
        model_idx = headers.index("claude_model") if "claude_model" in headers else -1
        text_idx = headers.index("role") if "role" in headers else -1
        tokens_idx = headers.index("max_token") if "max_token" in headers else -1
        for row in values[1:]:
            def cell(idx: int) -> str:
                return row[idx].strip() if 0 <= idx < len(row) else ""
            tag = cell(tag_idx).lower()
            if not tag:
                continue
            if cell(model_idx):
                step_models[tag] = cell(model_idx)
            if cell(text_idx):
                prompts[tag] = cell(text_idx).replace("\\n", "\n")
            if cell(tokens_idx).isdigit():
                step_max_tokens[tag] = int(cell(tokens_idx))

    def first(key: str) -> str:
        return columns.get(key, [""])[0] if columns.get(key) else ""

    severity_instructions = dict(zip(
        columns.get("check_severity", []),
        columns.get("flag_instruction", []),
    ))

    page_limits = {
        doc_type: int(limit)
        for doc_type, limit in zip(columns.get("document_type", []),
                                   columns.get("page_limit", []))
        if limit.isdigit()
    }

    # "Amber"/"Red" -> flag letters, paired with their word limits
    sentence_word_limits = {
        flag.strip().lower()[:1]: int(limit)
        for flag, limit in zip(columns.get("sentence_word_flag", []),
                               columns.get("sentence_word_limit", []))
        if limit.isdigit() and flag.strip()
    }

    return StyleGuideConfig(
        claude_model=first("claude_model") or first("claude_model_selection"),
        check_severity=first("check_severity"),
        document_types=columns.get("document_type", []),
        sections=columns.get("section", []),
        input_levels=columns.get("input_level", []),
        role_context=first("role_context"),
        severity_instructions=severity_instructions,
        step_models=step_models,
        prompts=prompts,
        report_link=" ".join(columns.get("report_link", [])),
        batching=first("batching").lower() == "yes",
        cache=first("cache").lower() == "yes",
        # verification defaults ON unless the config explicitly says "no"
        verify=first("verify").lower() != "no",
        mode=first("mode").lower(),
        max_pages=int(first("max_pages")) if first("max_pages").isdigit() else 0,
        max_report_cost_eur=(int(first("max_report_cost_eur"))
                             if first("max_report_cost_eur").isdigit() else 0),
        clear_ui=first("clear_ui").lower() == "yes",
        concurrency=(int(first("concurrency"))
                     if first("concurrency").isdigit() and int(first("concurrency")) > 0
                     else 8),
        page_limits=page_limits,
        step_max_tokens=step_max_tokens,
        sentence_word_limits=sentence_word_limits,
        raw=columns,
    )


def load_rules(sheet_id: str | None = None) -> list[Rule]:
    """Read the TE_style_rules tab.

    Column layout (by position):
      A include_AI_check (yes / no / coded) | B rule_tag (2-3 word summary) |
      C rule | D right (correct example) | E wrong (breaching example) |
      F level | G figure_type (header/sub_header/footer/whole_image) |
      H number_check | I hyperlink_rule | J (spacer) |
      K report | L briefing | M pr |
      N cover | O (spacer) | P executive summary | Q recommendations |
      R main text | S annex | T foreward (sic - sheet spelling)

    include_AI_check values:
      yes   — rule is checked via the AI loop
      no    — rule is inactive
      coded — always in the run, but implemented as a deterministic code
              check (figure width, one figure per subsection, footer length,
              full org name, sentence length); never sent to the AI
    """
    sheet_id = sheet_id or find_sheet_id()
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{RULES_TAB}'!A1:Z1000"
    ).execute().get("values", [])
    if len(values) < 2:
        raise RuntimeError(f"'{RULES_TAB}' tab is empty or missing.")

    doc_type_cols = {10: "report", 11: "briefing", 12: "pr"}
    # "foreward" keeps the sheet's spelling so the section string matches
    section_cols = {13: "cover", 15: "executive summary", 16: "recommendations",
                    17: "main text", 18: "annex", 19: "foreward"}

    rules: list[Rule] = []
    skipped: list[str] = []
    for row_num, row in enumerate(values[1:], start=2):
        def cell(idx: int) -> str:
            return row[idx].strip().lower() if idx < len(row) else ""

        def raw_cell(idx: int) -> str:
            return row[idx].strip() if idx < len(row) else ""

        include = cell(0)
        if include not in ("yes", "coded", "n/a", "na"):
            continue
        coded = include != "yes"
        text = raw_cell(2)
        level = RULE_LEVEL_TO_CHUNK_LEVEL.get(cell(5))
        if not text or level is None:
            skipped.append(f"row {row_num}: missing rule text or bad level {cell(5)!r}")
            continue
        rules.append(Rule(
            rule_id=f"rule-{row_num:03d}",
            category="",
            text=text,
            input_level=level,
            right=raw_cell(3),
            wrong=raw_cell(4),
            figure_type=cell(6).replace(" ", "_"),
            coded=coded,
            number_check=cell(7) == "yes",
            hyperlink_rule=cell(8) == "yes",
            rule_tag=raw_cell(1),
            document_types={name for idx, name in doc_type_cols.items() if cell(idx) == "yes"},
            sections={name for idx, name in section_cols.items() if cell(idx) == "yes"},
        ))
    logger = logging.getLogger(__name__)
    n_coded = sum(1 for r in rules if r.coded)
    if n_coded:
        logger.info("%d rule(s) marked coded - covered by deterministic checks", n_coded)
    if skipped:
        logger.warning("skipped %d malformed rule rows: %s", len(skipped), skipped)
    return rules
