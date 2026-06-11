"""Load the master_report_checker Google Sheet (config tab for now).

All keys and values are lowercased on read — the sheet is hand-edited and
case drifts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.auth import drive_service, sheets_service

SHEET_NAME = "master_report_checker"
CONFIG_TAB = "config"


@dataclass
class StyleGuideConfig:
    claude_model: str = ""
    check_severity: str = ""
    document_types: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    input_levels: list[str] = field(default_factory=list)
    raw: dict[str, list[str]] = field(default_factory=dict)


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
        claude_model=first("claude_model"),
        check_severity=first("check_severity"),
        document_types=columns.get("document_type", []),
        sections=columns.get("section", []),
        input_levels=columns.get("input_level", []),
        raw=columns,
    )
