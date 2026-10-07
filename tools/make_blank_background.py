#!/usr/bin/env python3
"""Turn a filled-in Canva certificate export into the blank background.

    python tools/make_blank_background.py my_canva_export.pdf

Writes background_blank.pdf (next to make_certificates.py) with all live text
removed but graphics left alone, and prints every text span's font, size, colour
and baseline so the layout constants at the top of make_certificates.py can be
updated if the design changed.

In Canva: export as PDF (Standard). The text must still be real text, not
flattened to paths, otherwise nothing is detected and nothing is removed.
"""
import sys
from pathlib import Path

import pymupdf

if len(sys.argv) != 2:
    sys.exit(__doc__)

src = pymupdf.open(sys.argv[1])
page = src[0]
print(f"page size: {page.rect.width:.0f} x {page.rect.height:.0f} pt, "
      f"fonts: {[f[3] for f in page.get_fonts()]}")

spans = [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]]
if not spans:
    sys.exit("No live text found in this PDF; re-export from Canva as a standard PDF.")

for s in spans:
    x0, _, x1, _ = s["bbox"]
    print(f"  baseline y={s['origin'][1]:6.1f}  size={s['size']:5.2f}  colour=#{s['color']:06x}  "
          f"centre x={(x0 + x1) / 2:6.1f}  {s['font']}  {s['text']!r}")
    page.add_redact_annot(pymupdf.Rect(s["bbox"]), fill=None)

page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
out = Path(__file__).resolve().parent.parent / "background_blank.pdf"
src.save(out, garbage=4, deflate=True)
print(f"wrote {out}")
