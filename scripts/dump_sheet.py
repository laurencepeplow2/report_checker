"""Dump every tab of the master_report_checker sheet to see its real layout."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.auth import sheets_service

SHEET_ID = "1i3N9ApR9cjOI-CjeoyrF9GWq3AcCd8njCT8gwrYV1V4"

meta = sheets_service().spreadsheets().get(spreadsheetId=SHEET_ID).execute()
print("Spreadsheet:", meta["properties"]["title"])
tab_names = [s["properties"]["title"] for s in meta["sheets"]]
print("Tabs:", tab_names)

for name in tab_names:
    values = sheets_service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{name}'!A1:Z60"
    ).execute().get("values", [])
    print(f"\n=== tab {name!r} ({len(values)} rows shown, first 60) ===")
    for i, row in enumerate(values, 1):
        print(f"  {i:3} {row}")
