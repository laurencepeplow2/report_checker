"""Extract header / subheader / footer text from figure images - no AI API.

Uses the Windows built-in OCR engine (winocr) to get text lines with
bounding boxes, then classifies blocks by position and type size:

  header    - top block in large (bold title) type
  subheader - the next block below the header in smaller type
              (subtitle or legend line)
  footer    - bottom block (typically "Source: ..."), excluding the
              T&E logo mark

Counts how many visual lines each block wraps over (the style guide caps
footers at two lines). Writes data/figure_text.csv.

Usage:
    python extract_figure_text.py
"""
from __future__ import annotations

import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path

import winocr
from PIL import Image

DATA_DIR = Path(__file__).resolve().parent / "data"
IMAGE_DIR = DATA_DIR / "images"
OUT_CSV = DATA_DIR / "figure_text.csv"

# Title type must be at least this much taller than the figure's median
# text to count as the header.
HEADER_HEIGHT_RATIO = 1.35
# Footer lives in the bottom band of the image.
FOOTER_BAND = 0.82
# The T&E logo mark OCRs as a short token like "T&E" / "3 T&E" - not footer text.
LOGO_RE = re.compile(r"^[^A-Za-z0-9]*\d?\s*T\s*&\s*E[^A-Za-z0-9]*$", re.IGNORECASE)


@dataclass
class Line:
    text: str
    x: float
    y: float
    height: float
    bottom: float


async def ocr_lines(image: Image.Image) -> list[Line]:
    result = await winocr.recognize_pil(image, "en-US")
    lines: list[Line] = []
    for raw in result.lines:
        words = list(raw.words)
        if not words:
            continue
        rects = [w.bounding_rect for w in words]
        x = min(r.x for r in rects)
        y = min(r.y for r in rects)
        bottom = max(r.y + r.height for r in rects)
        # median word height - legend squares / symbols inflate the max
        heights = sorted(r.height for r in rects)
        height = heights[len(heights) // 2]
        text = raw.text.replace("�", "").strip()
        if text:
            lines.append(Line(text, x, y, height, bottom))
    lines.sort(key=lambda l: l.y)
    return lines


SOURCE_RE = re.compile(r"^\W*(sourc|soure|note|\*)", re.IGNORECASE)


def alpha_words(text: str) -> int:
    return sum(1 for w in re.findall(r"[A-Za-z]{2,}", text))


def is_legend(text: str) -> bool:
    """Legend lines: colour swatches OCR as dashes/bullets, and series
    names are short ALL-CAPS tokens (LOW, EU, REF...) or marker-prefixed
    phrases ("— Commission proposal —Additional weakening..."). A prose
    subtitle that merely mentions acronyms ("...produced in the EU...")
    is NOT a legend - caps tokens only count when the line is mostly
    made of them."""
    markers = len(re.findall(r"[—•■▪-]\s*\w", text))
    caps_tokens = len(re.findall(r"\b[A-Z]{2,5}\b\s*\+?", text))
    mostly_tokens = alpha_words(text) <= caps_tokens + 2
    return markers >= 2 or (caps_tokens >= 2 and mostly_tokens) or bool(
        re.match(r"^\W*(risk category|legend)\b", text, re.IGNORECASE))


def group_block(lines: list[Line]) -> list[list[Line]]:
    """Split consecutive lines into blocks on vertical gaps > 1.6x line height."""
    blocks: list[list[Line]] = []
    for line in lines:
        if blocks and line.y - blocks[-1][-1].bottom < 1.6 * max(line.height, 8):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return blocks


def classify(lines: list[Line], img_height: int, img_width: int) -> dict:
    if not lines:
        return {"header": [], "subheader": [], "footer": []}

    heights = sorted(l.height for l in lines)
    median_h = heights[len(heights) // 2]

    # Header: consecutive large-type lines from the top of the image, all
    # close in size to the first (title) line.
    header: list[Line] = []
    rest_start = 0
    for i, line in enumerate(lines):
        if i != len(header) or line.y >= 0.35 * img_height:
            break
        if not header:
            # Seed: the T&E template always opens with the bold title, so the
            # topmost line counts even when huge data labels skew the median.
            ok = (line.height >= HEADER_HEIGHT_RATIO * median_h
                  or line.y < 0.12 * img_height)
        else:
            # Continuation: close in size to the tallest title line (the
            # legend and panel titles are bold too, but clearly smaller)
            # and directly below it.
            ok = (line.height >= 0.78 * max(l.height for l in header)
                  and line.y - header[-1].bottom < 1.8 * line.height)
        if not ok:
            break
        header.append(line)
        rest_start = i + 1

    # Subheader: the block sitting directly under the header (subtitle),
    # with legend lines split out into their own bucket. If the next text
    # is far below (chart content), there is none. In-chart furniture is
    # excluded: series labels hug the right edge, axis ticks are numeric.
    subheader: list[Line] = []
    legend: list[Line] = []
    if header:
        after = lines[rest_start:]
        blocks = group_block(after)
        # subtitle/legend can be the first block, or the first two when a
        # subtitle is followed by a separate legend row
        top_blocks = [
            b for b in blocks[:2]
            if b and b[0].y - header[-1].bottom < 4.5 * max(median_h, 10)
               and b[0].y < 0.45 * img_height
        ]
        for block_index, block in enumerate(top_blocks):
            for line in block:
                if line.x >= 0.45 * img_width or alpha_words(line.text) < 1:
                    continue  # in-chart furniture
                if is_legend(line.text):
                    legend.append(line)
                elif block_index == 0:
                    # subtitles only ever sit in the first block under the
                    # header; later blocks are legend rows or chart content
                    subheader.append(line)

    # Footer: bottom-band lines, logo excluded. Prefer everything from the
    # first "Source:/Note:" line down; otherwise fall back to prose lines
    # (>=4 words) - axis ticks and category labels are numeric/short.
    band = [
        l for l in lines
        if l.y >= FOOTER_BAND * img_height and not LOGO_RE.match(l.text)
    ]
    source_idx = next((i for i, l in enumerate(band) if SOURCE_RE.match(l.text)), None)
    if source_idx is not None:
        footer = band[source_idx:]
    else:
        footer = [l for l in band if alpha_words(l.text) >= 4]

    return {"header": header, "subheader": subheader, "legend": legend,
            "footer": footer}


def block_text(block: list[Line]) -> str:
    return " ".join(l.text for l in block)


async def main() -> None:
    images = sorted(IMAGE_DIR.glob("*.png"))
    if not images:
        raise SystemExit(f"No images in {IMAGE_DIR} - run ingest/test_run first.")

    rows = []
    for path in images:
        image = Image.open(path).convert("RGB")
        lines = await ocr_lines(image)
        blocks = classify(lines, image.height, image.width)
        row = {"image": path.name, "ocr_lines_total": len(lines)}
        for name in ("header", "subheader", "legend", "footer"):
            block = blocks[name]
            row[f"{name}_text"] = block_text(block)
            row[f"{name}_lines"] = len(block)
        row["footer_over_2_lines"] = "yes" if row["footer_lines"] > 2 else "no"
        rows.append(row)
        print(f"{path.name}: header {row['header_lines']}L | "
              f"subheader {row['subheader_lines']}L | "
              f"legend {row['legend_lines']}L | footer {row['footer_lines']}L")
        print(f"   H: {row['header_text'][:90]}")
        print(f"   S: {row['subheader_text'][:90]}")
        print(f"   L: {row['legend_text'][:90]}")
        print(f"   F: {row['footer_text'][:90]}")

    out = OUT_CSV
    try:
        f = out.open("w", newline="", encoding="utf-8-sig")
    except PermissionError:
        # the CSV is open in Excel - write alongside it instead of failing
        from datetime import datetime
        out = OUT_CSV.with_name(
            f"figure_text_{datetime.now():%H%M%S}.csv")
        f = out.open("w", newline="", encoding="utf-8-sig")
        print(f"WARNING: {OUT_CSV.name} is locked (open in Excel?) - "
              f"writing {out.name} instead")
    with f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n{len(rows)} figures -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
