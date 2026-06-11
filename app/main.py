"""FastAPI app serving the reviewer UI over the latest test run."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="T&E Report Checker")


@app.get("/api/run-data")
def run_data() -> dict:
    path = DATA_DIR / "test_run.json"
    if not path.exists():
        raise HTTPException(404, "No run data yet - run test_run.py first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    for chunk in data["chunks"]:
        if chunk.get("image"):
            chunk["image"] = f"/data/images/{Path(chunk['image']).name}"
    return data


@app.get("/api/analysis")
def analysis() -> dict:
    path = DATA_DIR / "analysis.json"
    if not path.exists():
        raise HTTPException(404, "No analysis yet - run analyse_doc.py first.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
