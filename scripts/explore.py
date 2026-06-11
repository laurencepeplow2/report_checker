"""One-off exploration: dump the test doc's tab structure and the
master_report_checker config tab, so the parser can be grounded in real data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import docs_service, drive_service, sheets_service

DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def paragraph_text(paragraph: dict) -> str:
    return "".join(
        el.get("textRun", {}).get("content", "")
        for el in paragraph.get("elements", [])
    )


def summarise_tab(tab: dict, depth: int = 0) -> None:
    props = tab.get("tabProperties", {})
    body = tab.get("documentTab", {}).get("body", {}).get("content", [])
    n_paras = n_headings = n_images = n_tables = 0
    first_lines: list[str] = []
    for element in body:
        if "paragraph" in element:
            para = element["paragraph"]
            style = para.get("paragraphStyle", {}).get("namedStyleType", "")
            text = paragraph_text(para).strip()
            if text:
                n_paras += 1
                if len(first_lines) < 5:
                    first_lines.append(f"[{style}] {text[:100]}")
            if style.startswith("HEADING"):
                n_headings += 1
            for el in para.get("elements", []):
                if "inlineObjectElement" in el:
                    n_images += 1
        elif "table" in element:
            n_tables += 1
    indent = "  " * depth
    print(f"{indent}TAB '{props.get('title')}' (id={props.get('tabId')}): "
          f"{n_paras} paras, {n_headings} headings, {n_images} inline objects, {n_tables} tables")
    for line in first_lines:
        print(f"{indent}    {line}")
    for child in tab.get("childTabs", []):
        summarise_tab(child, depth + 1)


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    doc = docs_service().documents().get(
        documentId=DOC_ID, includeTabsContent=True
    ).execute()
    (DATA_DIR / "raw_doc.json").write_text(
        json.dumps(doc, indent=1), encoding="utf-8"
    )
    print(f"=== Document: {doc.get('title')} ===")
    tabs = doc.get("tabs", [])
    print(f"{len(tabs)} top-level tabs\n")
    for tab in tabs:
        summarise_tab(tab)

    print("\n=== Searching Drive for master_report_checker ===")
    resp = drive_service().files().list(
        q="name contains 'master_report_checker' and trashed = false",
        fields="files(id, name, mimeType)",
    ).execute()
    for f in resp.get("files", []):
        print(f"  {f['name']}  ({f['mimeType']})  id={f['id']}")

    if resp.get("files"):
        sheet_id = resp["files"][0]["id"]
        meta = sheets_service().spreadsheets().get(spreadsheetId=sheet_id).execute()
        tab_names = [s["properties"]["title"] for s in meta["sheets"]]
        print(f"\n  Sheet tabs: {tab_names}")
        for name in tab_names:
            values = sheets_service().spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"'{name}'!A1:Z40"
            ).execute().get("values", [])
            print(f"\n  --- tab '{name}' (first 40 rows) ---")
            for row in values:
                print("   ", row)


if __name__ == "__main__":
    main()
