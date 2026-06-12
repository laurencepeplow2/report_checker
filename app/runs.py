"""Per-report output store.

Each checked document gets its own folder, data/runs/<doc_id>/, holding
test_run.json/csv and analysis.json. data/runs/index.json lists every
report so the UI can offer a selector and exports can loop.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RUNS_DIR = DATA_DIR / "runs"
INDEX_PATH = RUNS_DIR / "index.json"


def run_dir(doc_id: str) -> Path:
    path = RUNS_DIR / doc_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_index() -> list[dict]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def update_index(doc_id: str, title: str, **fields) -> None:
    """Merge fields into the report's index entry (creates it if new)."""
    index = _load_index()
    entry = next((e for e in index if e.get("doc_id") == doc_id), None)
    if entry is None:
        entry = {"doc_id": doc_id}
        index.append(entry)
    entry["title"] = title
    entry["updated"] = datetime.now().isoformat(timespec="seconds")
    entry.update(fields)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def list_reports() -> list[dict]:
    return _load_index()


def load_edits(doc_id: str) -> dict:
    """Committed edits per chunk: {chunk_id: {at, text}}. One edit per
    paragraph - once committed, the chunk is locked."""
    path = RUNS_DIR / doc_id / "edits.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def record_edit(doc_id: str, chunk_id: str, text: str) -> None:
    edits = load_edits(doc_id)
    edits[chunk_id] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "text": text,
    }
    path = run_dir(doc_id) / "edits.json"
    path.write_text(json.dumps(edits, indent=2, ensure_ascii=False),
                    encoding="utf-8")
