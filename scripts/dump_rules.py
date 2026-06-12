"""Dump the TE_style_rules tab layout."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.auth import sheets_service

SHEET_ID = "1i3N9ApR9cjOI-CjeoyrF9GWq3AcCd8njCT8gwrYV1V4"

values = sheets_service().spreadsheets().values().get(
    spreadsheetId=SHEET_ID, range="'TE_style_rules'!A1:M60"
).execute().get("values", [])

print("HEADER:", values[0])
for i, row in enumerate(values[1:25], start=2):
    cells = [row[j] if j < len(row) else "" for j in range(12)]
    print(f"{i:3} | {cells[0]:6} | {cells[1][:50]:50} | {cells[2]:10} | "
          f"{cells[3]:12} | {' '.join(cells[4:])[:40]}")
