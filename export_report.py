"""Build a sendable, self-contained report folder.

Bundles the UI (HTML/CSS/JS), the latest run data and analysis, and all
figure images (base64-inlined) into one file:

    output/<doc title>/Open AI Report Check.html

The recipient downloads the folder (or just the file) and double-clicks -
no server, no Python, no setup. "Checked" progress boxes still work and
persist in their browser.

Usage:
    python export_report.py
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
OUTPUT = ROOT / "output"


def image_data_uri(path: str) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return "data:image/png;base64," + base64.standard_b64encode(p.read_bytes()).decode()


def main() -> None:
    run_path = DATA / "test_run.json"
    if not run_path.exists():
        raise SystemExit("No run data - run test_run.py first.")
    run = json.loads(run_path.read_text(encoding="utf-8"))

    analysis = {}
    analysis_path = DATA / "analysis.json"
    if analysis_path.exists():
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))

    # Inline figure images so the file is fully portable
    for chunk in run["chunks"]:
        if chunk.get("image"):
            chunk["image"] = image_data_uri(chunk["image"])

    payload = json.dumps({"run": run, "analysis": analysis}, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")  # never close the script tag early

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    html = html.replace(
        '<link rel="stylesheet" href="/static/styles.css">',
        f"<style>\n{css}\n</style>",
    )
    logo = STATIC / "te_logo.webp"
    if logo.exists():
        logo_uri = ("data:image/webp;base64,"
                    + base64.standard_b64encode(logo.read_bytes()).decode())
        html = html.replace('src="/static/te_logo.webp"', f'src="{logo_uri}"')
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"<script>window.__RC_DATA__ = {payload};</script>\n<script>\n{js}\n</script>",
    )

    title = run.get("title", "report")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_") or "report"
    out_dir = OUTPUT / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "Open AI Report Check.html"
    out_file.write_text(html, encoding="utf-8")
    print(f"-> {out_file}")
    print(f"   ({out_file.stat().st_size / 1024:.0f} KB, fully self-contained - "
          "send the folder or just this file)")


if __name__ == "__main__":
    main()
