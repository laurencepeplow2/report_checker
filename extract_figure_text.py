"""CLI: extract header/subheader/legend/footer text from every figure
image into data/figure_text.csv. Logic lives in app/figure_parts.py.

Usage:
    python extract_figure_text.py
"""
from __future__ import annotations

import asyncio

from app.figure_parts import main

if __name__ == "__main__":
    asyncio.run(main())
