"""Check which Google APIs are enabled for the service account project."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import credentials, docs_service, drive_service, sheets_service

DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"

print("service account:", credentials().service_account_email)

try:
    resp = drive_service().files().list(
        q="trashed = false", fields="files(id, name, mimeType)", pageSize=20
    ).execute()
    print("DRIVE OK — files shared with the service account:")
    for f in resp.get("files", []):
        print("  ", f["name"], "|", f["mimeType"], "|", f["id"])
except Exception as e:  # noqa: BLE001
    print("DRIVE FAIL:", str(e)[:250])

try:
    docs_service().documents().get(documentId=DOC_ID).execute()
    print("DOCS OK")
except Exception as e:  # noqa: BLE001
    print("DOCS FAIL:", str(e)[:250])

try:
    resp = drive_service().files().list(
        q="name contains 'master_report_checker' and trashed = false",
        fields="files(id, name)",
    ).execute()
    files = resp.get("files", [])
    if files:
        sheets_service().spreadsheets().get(spreadsheetId=files[0]["id"]).execute()
        print("SHEETS OK")
    else:
        print("SHEETS: master_report_checker not visible to service account")
except Exception as e:  # noqa: BLE001
    print("SHEETS FAIL:", str(e)[:250])
