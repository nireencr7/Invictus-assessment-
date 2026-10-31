"""Renders docs/ARCHITECTURE.md to docs/ARCHITECTURE.pdf (the 2-page note) via headless Chrome.

uv run --with markdown python scripts/render_architecture_pdf.py
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

CSS = """
@page { size: A4; margin: 11mm 12mm; }
body { font: 8.6pt/1.36 -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; color: #1f2328; }
h1 { font-size: 14.5pt; margin: 0 0 5px; }
h2 { font-size: 10.5pt; margin: 9px 0 3px; color: #0b3d91; border-bottom: 1px solid #d0d7de; padding-bottom: 2px; }
p { margin: 3px 0 5px; }
ol { margin: 3px 0 5px 16px; padding: 0; } li { margin: 1px 0; }
table { border-collapse: collapse; width: 100%; margin: 3px 0 5px; font-size: 8pt; }
th, td { border: 1px solid #d0d7de; padding: 2.5px 5px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
code { font: 7.8pt ui-monospace, Menlo, Consolas, monospace; background: #f3f4f6; padding: 0 2px; border-radius: 3px; }
svg { width: 100%; height: auto; display: block; margin: 2px 0 4px; }
a { color: #0b3d91; text-decoration: none; }
.foot { margin-top: 6px; font-size: 7.5pt; color: #57606a; }
"""


def main() -> None:
    md = (DOCS / "ARCHITECTURE.md").read_text()
    md = md.replace("![Architecture](architecture.svg)", "@@SVG@@")
    html = markdown.markdown(md, extensions=["tables"])
    svg = (DOCS / "architecture.svg").read_text()
    html = html.replace("<p>@@SVG@@</p>", svg)
    html = re.sub(r'href="RESULTS.md"', 'href="RESULTS.md"', html)
    doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html}</body></html>"
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        sys.exit("needs Chrome/Chromium on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "architecture.html"
        src.write_text(doc)
        out = DOCS / "ARCHITECTURE.pdf"
        subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out}",
                src.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
