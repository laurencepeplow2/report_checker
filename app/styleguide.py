"""Load the master_report_checker Google Sheet (config + TE_style_rules tabs).

All keys and values are lowercased on read — the sheet is hand-edited and
case drifts. The model id keeps its original case but is whitespace-stripped.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.auth import drive_service, sheets_service

SHEET_NAME = "master_report_checker"
CONFIG_TAB = "config"
RULES_TAB = "TE_style_rules"

# The rules tab maps "figures" to what the parser calls a "figure" chunk.
RULE_LEVEL_TO_CHUNK_LEVEL = {
    "paragraph": "paragraph",
    "figures": "figure",
}


@dataclass
class StyleGuideConfig:
    claude_model: str = ""
    check_severity: str = ""
    document_types: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    input_levels: list[str] = field(default_factory=list)
    raw: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Rule:
    rule_id: str            # row-derived, e.g. "rule-002" (sheet row number)
    category: str
    text: str
    input_level: str        # normalised chunk level: paragraph | figure | subsection
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


def load_config(sheet_id: str | None = None) -> StyleGuideConfig:
    """Read the config tab.

    Layout assumption (validated against the real sheet): row 1 holds
    variable names, values listed beneath each header. Single-value
    variables (claude_model, check_severity) take the first value; list
    variables keep every non-empty cell.
    """
    sheet_id = sheet_id or find_sheet_id()
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{CONFIG_TAB}'!A1:Z100"
    ).execute().get("values", [])
    if not values:
        raise RuntimeError(f"'{CONFIG_TAB}' tab is empty or missing.")

    headers = [h.strip().lower() for h in values[0]]
    columns: dict[str, list[str]] = {h: [] for h in headers if h}
    for row in values[1:]:
        for idx, header in enumerate(headers):
            if header and idx < len(row) and row[idx].strip():
                columns[header].append(row[idx].strip().lower())

    def first(key: str) -> str:
        return columns.get(key, [""])[0] if columns.get(key) else ""

    return StyleGuideConfig(
        claude_model=first("claude_model_selection"),
        check_severity=first("check_severity"),
        document_types=columns.get("document_type", []),
        sections=columns.get("section", []),
        input_levels=columns.get("input_level", []),
        raw=columns,
    )


def load_rules(sheet_id: str | None = None) -> list[Rule]:
    """Read the TE_style_rules tab.

    Column layout (by position — the header of column D currently reads
    "report" but the column holds the input level):
      A include (yes/no) | B category | C rules | D input_level |
      E report | F briefing | G pr |
      H cover | I executive summary | J main text | K annex
    """
    sheet_id = sheet_id or find_sheet_id()
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{RULES_TAB}'!A1:K1000"
    ).execute().get("values", [])
    if len(values) < 2:
        raise RuntimeError(f"'{RULES_TAB}' tab is empty or missing.")

    doc_type_cols = {4: "report", 5: "briefing", 6: "pr"}
    section_cols = {7: "cover", 8: "executive summary", 9: "main text", 10: "annex"}

    rules: list[Rule] = []
    skipped: list[str] = []
    for row_num, row in enumerate(values[1:], start=2):
        def cell(idx: int) -> str:
            return row[idx].strip().lower() if idx < len(row) else ""

        if cell(0) != "yes":
            continue
        text = (row[2].strip() if len(row) > 2 else "")
        level = RULE_LEVEL_TO_CHUNK_LEVEL.get(cell(3))
        if not text or level is None:
            skipped.append(f"row {row_num}: missing rule text or bad input_level {cell(3)!r}")
            continue
        rules.append(Rule(
            rule_id=f"rule-{row_num:03d}",
            category=" ".join((row[1] if len(row) > 1 else "").split()),
            text=text,
            input_level=level,
            document_types={name for idx, name in doc_type_cols.items() if cell(idx) == "yes"},
            sections={name for idx, name in section_cols.items() if cell(idx) == "yes"},
        ))
    if skipped:
        print(f"WARNING: skipped {len(skipped)} malformed rule rows: {skipped}")
    return rules
