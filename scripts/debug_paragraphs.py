"""Find paragraph chunks that appear to start mid-sentence and inspect the
raw document structure around them to identify the cause."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DATA = Path(__file__).resolve().parent.parent / "data"

chunks = json.loads((DATA / "chunks.json").read_text(encoding="utf-8"))["chunks"]
paras = [c for c in chunks if c["input_level"] == "paragraph" and c["kind"] == "text"]

lower_start = [c for c in paras if c["text"][:1].islower()]
print(f"{len(paras)} text paragraphs; {len(lower_start)} start lowercase\n")
for c in lower_start[:12]:
    print(f"--- {c['chunk_id']} | {c['tab_title']}\n    starts: {c['text'][:100]!r}")

print("\n=== consecutive pairs where the first does not end a sentence:")
enders = (".", "!", "?", ":", ";", "”", '"', "…", ")")
prev = None
n = 0
for c in paras:
    if prev and prev["tab_title"] == c["tab_title"]:
        if not prev["text"].rstrip().endswith(enders):
            n += 1
            if n <= 12:
                print(f"--- {prev['chunk_id']} ends:   {prev['text'][-80:]!r}")
                print(f"    {c['chunk_id']} starts: {c['text'][:80]!r}\n")
    prev = c
print(f"{n} suspicious pairs")

# Look at the raw doc around the first suspicious chunk to see structure
raw_path = DATA / "raw_doc.json"
if raw_path.exists() and lower_start:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    target = lower_start[0]["text"][:40]
    print(f"\n=== raw structural elements around {target!r}:")
    for tab in raw.get("tabs", []):
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        for i, element in enumerate(body):
            if "paragraph" not in element:
                continue
            text = "".join(
                el.get("textRun", {}).get("content", "")
                for el in element["paragraph"].get("elements", [])
            )
            if target in text:
                for j in range(max(0, i - 2), min(len(body), i + 2)):
                    e = body[j]
                    if "paragraph" in e:
                        p = e["paragraph"]
                        style = p.get("paragraphStyle", {}).get("namedStyleType")
                        bullet = "bullet" if p.get("bullet") else ""
                        t = "".join(
                            el.get("textRun", {}).get("content", "")
                            for el in p.get("elements", [])
                        )
                        print(f"  [{j}] {style} {bullet} {t[:90]!r}")
                    else:
                        print(f"  [{j}] {list(e.keys())}")
                break
