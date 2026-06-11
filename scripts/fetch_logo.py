"""Download the T&E header logo via its signed CDN URL."""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0",
      "Referer": "https://www.transportenvironment.org/"}

home = requests.get("https://www.transportenvironment.org/", headers=UA, timeout=30).text

match = re.search(r'<img[^>]+logo[^>]+>', home, re.IGNORECASE)
src = html.unescape(re.search(r'src="([^"]+)"', match.group(0)).group(1))
print("signed src:", src[:160])

resp = requests.get(src, headers=UA, timeout=30)
resp.raise_for_status()
content_type = resp.headers.get("content-type", "")
ext = "webp" if "webp" in content_type else "png"
out = ROOT / "static" / f"te_logo.{ext}"
out.write_bytes(resp.content)
print(f"saved {out.name} ({len(resp.content)} bytes, {content_type})")

stale = ROOT / "static" / "te_logo.svg"
if stale.exists():
    stale.unlink()  # previous attempt grabbed a carousel dot, not the logo
