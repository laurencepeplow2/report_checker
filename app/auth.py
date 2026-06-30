"""Shared Google API clients built from the service account key."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv()

SCOPES = [
    # write scope: editor mode commits reviewed suggestions to the doc
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
    # drive.file: create + trash ONLY app-created files (the synthetic doc the
    # integration test makes); does not grant access to the user's other files
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def credentials() -> Credentials:
    key_file = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
    key_path = PROJECT_ROOT / key_file
    return Credentials.from_service_account_file(str(key_path), scopes=SCOPES)


@lru_cache(maxsize=1)
def docs_service():
    return build("docs", "v1", credentials=credentials())


@lru_cache(maxsize=1)
def drive_service():
    return build("drive", "v3", credentials=credentials())


@lru_cache(maxsize=1)
def sheets_service():
    return build("sheets", "v4", credentials=credentials())
