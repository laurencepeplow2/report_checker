"""Assemble ONE self-contained folder to hand to a colleague so they can run
the full tool - including the live edit / commit-to-Google-Docs feature.

Unlike export_report.py (a read-only HTML), this bundles everything needed to
RUN the app: the code, your credentials, offline dependency wheels, and a
double-click launcher. The recipient needs only Windows + Python installed;
they never have to clone the repo or fetch anything else.

    python build_share.py

Output: share/report_checker/   (zip it and send)

What goes in:
  - app/, static/ and the runnable scripts
  - requirements.txt + wheels/ (offline install; falls back to online)
  - service_account.json and .env  (SECRETS - share via a secure channel)
  - START_EDITOR.bat / RUN_CHECK.bat / READ_ME_FIRST.txt
Local-only junk (.venv, data, output, __pycache__, .git) is left out.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "share" / "report_checker"

# code the app needs to run
CODE_DIRS = ["app", "static"]
CODE_FILES = ["requirements.txt", "README.md", "test_run.py", "analyse_doc.py",
              "export_report.py", "extract_figure_text.py", "ingest.py"]
SECRETS = ["service_account.json", ".env"]

START_BAT = r"""@echo off
REM One-click: set up (first run) then launch the reviewer UI with editing.
cd /d "%~dp0"
if not exist .venv (
  echo Creating environment (first run only)...
  py -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  echo Installing dependencies...
  pip install --no-index --find-links wheels -r requirements.txt 2>nul || pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
echo Starting the reviewer UI at http://localhost:8077  (close this window to stop)
start "" http://localhost:8077
python -m uvicorn app.main:app --port 8077
"""

RUN_CHECK_BAT = r"""@echo off
REM Check a document before editing. Pass the Google Doc id, or leave blank to
REM use the report_link in the master_report_checker sheet.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python test_run.py %1
python analyse_doc.py %1
echo.
echo Done. Now run START_EDITOR.bat (or refresh the browser) and use Edit mode.
pause
"""

READ_ME = """REPORT CHECKER - shared copy (with editing)
===========================================

You need: Windows 10/11 and Python 3.11+ installed (python.org). Nothing else.

FIRST TIME
  1. Unzip this folder somewhere (e.g. Documents).
  2. In Google Drive, share the document(s) you want to EDIT with this address
     as *Editor*:
         (see "client_email" in service_account.json)
     Also share the "master_report_checker" sheet with it (Viewer is enough).
  3. Double-click START_EDITOR.bat. The first run sets things up (a minute or
     two), then opens http://localhost:8077.

TO CHECK + EDIT A DOCUMENT
  - Put the Google Doc link in the report_link cell of the master_report_checker
    sheet, then double-click RUN_CHECK.bat.
    (Or run:  RUN_CHECK.bat <doc_id>  to check one specific document.)
  - Then in the browser (http://localhost:8077): Review tab -> Edit mode ->
    accept/reject changes -> Commit edit. Commit writes the paragraph back to
    the live Google Doc.

NOTES
  - Editing needs Editor access on the doc (step 2). Viewer only allows checking.
  - Tables and figures are flagged but edited by hand (not auto-committed).
  - The included API key bills the original owner's Anthropic account unless you
    replace ANTHROPIC_API_KEY in the .env file with your own.
"""


def main() -> None:
    if not all((ROOT / s).exists() for s in SECRETS):
        missing = [s for s in SECRETS if not (ROOT / s).exists()]
        print(f"!! missing {missing} - can't build a runnable share without them.")
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in CODE_DIRS:
        if (ROOT / d).exists():
            shutil.copytree(ROOT / d, DEST / d, ignore=ignore)
    for f in CODE_FILES + SECRETS:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, DEST / f)

    # offline dependency wheels (best-effort; START_EDITOR falls back to online)
    wheels = DEST / "wheels"
    wheels.mkdir()
    print("Downloading dependency wheels for offline install...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "download",
                        "-r", str(ROOT / "requirements.txt"), "-d", str(wheels)],
                       check=True)
    except Exception as exc:  # noqa: BLE001
        print(f"   (wheel download failed: {exc} - recipient will install online)")

    (DEST / "START_EDITOR.bat").write_text(START_BAT, encoding="utf-8")
    (DEST / "RUN_CHECK.bat").write_text(RUN_CHECK_BAT, encoding="utf-8")
    (DEST / "READ_ME_FIRST.txt").write_text(READ_ME, encoding="utf-8")

    size_mb = sum(f.stat().st_size for f in DEST.rglob("*") if f.is_file()) / 1e6
    print(f"\n-> {DEST}  ({size_mb:.0f} MB)")
    print("   Zip this folder and send it. Recipient: unzip, then double-click "
          "START_EDITOR.bat.")
    print("   NOTE: it contains service_account.json + .env (secrets) - send "
          "over a secure channel.")


if __name__ == "__main__":
    main()
