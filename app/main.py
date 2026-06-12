"""FastAPI app serving the reviewer UI over the latest runs.

Multi-report aware: each checked document lives in data/runs/<doc_id>/;
/api/reports lists them and /api/run-data + /api/analysis take ?doc=.
Falls back to the legacy single-report files in data/ when no runs
folder exists yet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.runs import RUNS_DIR, list_reports

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="T&E Report Checker")


def _data_file(name: str, doc: str | None) -> Path:
    if doc:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", doc):
            raise HTTPException(400, "Invalid report id.")
        path = RUNS_DIR / doc / name
        if path.exists():
            return path
        raise HTTPException(404, f"No {name} for report {doc} yet.")
    reports = list_reports()
    if reports:
        path = RUNS_DIR / reports[0]["doc_id"] / name
        if path.exists():
            return path
    legacy = DATA_DIR / name
    if legacy.exists():
        return legacy
    raise HTTPException(404, f"No {name} yet - run the pipeline first.")


@app.get("/api/reports")
def reports() -> list[dict]:
    return list_reports()


@app.get("/api/run-data")
def run_data(doc: str | None = None) -> dict:
    path = _data_file("test_run.json", doc)
    data = json.loads(path.read_text(encoding="utf-8"))
    for chunk in data["chunks"]:
        if chunk.get("image"):
            chunk["image"] = f"/data/images/{Path(chunk['image']).name}"
    return data


@app.get("/api/analysis")
def analysis(doc: str | None = None) -> dict:
    path = _data_file("analysis.json", doc)
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
