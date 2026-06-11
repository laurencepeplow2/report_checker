"""One-off: pull T&E's stylesheet colours and download the logo."""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"}

home = requests.get("https://www.transportenvironment.org/", headers=UA, timeout=30).text

# stylesheet URLs
css_urls = re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', home)
css_urls += re.findall(r'href="([^"]+\.css[^"]*)"', home)
print("CSS files:", css_urls[:5])

text = home
for url in dict.fromkeys(css_urls[:4]):
    if url.startswith("/"):
        url = "https://www.transportenvironment.org" + url
    try:
        text += requests.get(url, headers=UA, timeout=30).text
    except Exception as exc:  # noqa: BLE001
        print("  skip", url, exc)

# also inline styles / tailwind config colours
hexes = Counter(h.lower() for h in re.findall(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", text))
print("\nMost common hex colours:")
for colour, n in hexes.most_common(25):
    print(f"  {colour}  x{n}")

# CSS custom properties that look like brand colours
for match in re.findall(r"--[\w-]*(?:primary|brand|pink|accent|secondary)[\w-]*\s*:\s*[^;]+", text)[:20]:
    print(" ", match)

# download logo
logo_url = "https://transforms.transportenvironment.org/production/images/TE_Primary-logo_CMYK_Dark-pink.png"
resp = requests.get(logo_url, headers=UA, timeout=30)
resp.raise_for_status()
(ROOT / "static" / "te_logo.png").write_bytes(resp.content)
print(f"\nLogo saved: static/te_logo.png ({len(resp.content)} bytes)")
