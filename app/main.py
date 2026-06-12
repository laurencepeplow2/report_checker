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

from app.runs import RUNS_DIR, list_reports, load_edits, record_edit

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


@app.get("/api/edits")
def edits(doc: str | None = None) -> dict:
    doc_id = doc or _default_doc_id()
    return load_edits(doc_id) if doc_id else {}


@app.post("/api/commit-edit")
def commit_edit(payload: dict) -> dict:
    """Apply a reviewed suggestion to the live Google Doc. One commit per
    paragraph - committed chunks are locked."""
    doc_id = payload.get("doc") or _default_doc_id()
    chunk_id = payload.get("chunk_id", "")
    runs = payload.get("runs", [])
    if not doc_id or not chunk_id or not isinstance(runs, list) or not runs:
        raise HTTPException(400, "doc, chunk_id and runs are required.")

    if chunk_id in load_edits(doc_id):
        raise HTTPException(409, "This paragraph has already been changed - "
                                 "it is locked against further edits.")

    run_path = _data_file("test_run.json", payload.get("doc"))
    data = json.loads(run_path.read_text(encoding="utf-8"))
    chunk = next((c for c in data["chunks"] if c["chunk_id"] == chunk_id), None)
    if chunk is None:
        raise HTTPException(404, "Unknown chunk for this report.")
    if chunk["input_level"] != "paragraph":
        raise HTTPException(400, "Only paragraph extracts can be committed.")
    if chunk.get("kind") == "table" or " | " in chunk["text"]:
        raise HTTPException(400, "This extract is a table - tables can't be "
                                 "committed automatically; edit the doc by hand.")

    from app.doc_editor import EditError, apply_edit
    try:
        result = apply_edit(data["doc_id"], chunk.get("tab_id", ""),
                            chunk["text"], runs)
    except EditError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:  # noqa: BLE001 — surface API errors readably
        detail = str(exc)
        if "403" in detail or "permission" in detail.lower():
            detail = ("The service account does not have edit access to the "
                      "document - share it with Editor permission.")
        raise HTTPException(502, detail)

    record_edit(doc_id, chunk_id, result["new_text"])
    return {"ok": True, "new_text": result["new_text"]}


def _default_doc_id() -> str:
    reports = list_reports()
    return reports[0]["doc_id"] if reports else ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
