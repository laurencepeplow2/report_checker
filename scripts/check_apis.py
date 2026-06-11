"""Check which Google APIs are enabled and what's shared with the service account."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import credentials, docs_service, drive_service, sheets_service

DOC_ID = "1dyLbq5hMDUJlK9mUszUcUYAxzmo80To0h3n7ar-_B_8"

print("service account:", credentials().service_account_email)

# Can Drive see the test doc itself? (sharing check, independent of Docs API)
try:
    f = drive_service().files().get(fileId=DOC_ID, fields="id, name, mimeType").execute()
    print(f"DOC SHARED OK: {f['name']!r} ({f['mimeType']})")
except Exception as e:  # noqa: BLE001
    print("DOC NOT SHARED:", str(e)[:200])

# Docs API enabled?
try:
    docs_service().documents().get(documentId=DOC_ID).execute()
    print("DOCS API OK")
except Exception as e:  # noqa: BLE001
    print("DOCS API FAIL:", str(e)[:200])

# All spreadsheets visible to the service account
try:
    resp = drive_service().files().list(
        q="mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false",
        fields="files(id, name)", pageSize=50,
    ).execute()
    print("Spreadsheets visible:")
    for f in resp.get("files", []):
        print("  ", f["name"], "|", f["id"])
except Exception as e:  # noqa: BLE001
    print("DRIVE FAIL:", str(e)[:200])
