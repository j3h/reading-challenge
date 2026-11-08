"""The print PDF: for each classroom, a cover page (checklist for bagging prizes) then its certificates.

All pages are letter landscape, like the certificates, so the whole file prints in one orientation.
Classrooms are in alphabetical order by teacher; children alphabetical within, on the cover page and
in the certificate stack, so the checklist and the stack line up.

The certificate drawing itself is `make_certificates.draw_certificate`, unchanged.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pymupdf

import make_certificates as mc
from rc import checkpoint as ck

PAGE_W, PAGE_H = 792, 612
MARGIN = 48
BOTTOM = 570                      # lowest allowed baseline on a cover page
GREY = (0.45, 0.45, 0.45)
RULE = (0.8, 0.8, 0.8)

# table columns (x positions); numbers are right-aligned at their x
COL_BOX, COL_NAME, COL_MONTH, COL_TOTAL, COL_PRIZE = 48, 70, 350, 420, 444
NAME_W = COL_MONTH - COL_NAME - 52 - 8   # leaves room for a 7-character page count
PRIZE_W = PAGE_W - MARGIN - COL_PRIZE
SIZE, LINE_H, ROW_PAD = 11.5, 14.0, 7.0


def _wrap_items(t, items, width, size):
    """Greedy wrap of prize item names, comma separated, never splitting one item."""
    lines, cur = [], ""
    for i, item in enumerate(items):
        piece = item + ("," if i < len(items) - 1 else "")
        trial = f"{cur} {piece}".strip()
        if cur and t.width("body", trial, size) > width:
            lines.append(cur)
            cur = piece
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _wrap_name(t, name, width, size):
    """Name as one line, or wrapped on spaces; the font shrinks only if a single word is too wide."""
    words = name.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if cur and t.width("body", trial, size) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    widest = max(t.width("body", ln, size) for ln in lines)
    if widest > width:
        size = max(8.0, size * width / widest)
    return lines, size


def prize_tally(results) -> list[tuple[str, int]]:
    """[(prize item, how many to bag)] in the order of the levels table (smallest prize first)."""
    counts = Counter(p[2] for r in results for p in r.prizes)
    order = {}
    for threshold, _, prize in mc.load_levels():
        order.setdefault(prize, threshold)
    return sorted(counts.items(), key=lambda kv: (order.get(kv[0], 10**9), kv[0]))


def _text(t, x, y, text, size=SIZE, key="body", color=mc.BLACK):
    t.page.insert_text((x, y), text, fontname=t.fonts[key][0], fontsize=size, color=color)


def _right(t, xr, y, text, size=SIZE, color=mc.BLACK):
    _text(t, xr - t.width("body", text, size), y, text, size, color=color)


def _fit(t, key, text, size, min_size, width):
    """Largest size <= `size` (not below `min_size`) at which `text` fits `width`."""
    w = t.width(key, text, size)
    return size if w <= width else max(min_size, size * width / w)


def draw_cover_pages(doc, teacher, results, month, warn) -> int:
    """Append the cover page(s) for one classroom. Returns how many pages were added."""
    pages_added = 0
    page = None
    t = None

    def new_page(first):
        nonlocal page, t, pages_added
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        t = mc.Typesetter(page, doc)
        pages_added += 1
        if first:
            size = _fit(t, "name", teacher, 40, 24, PAGE_W - 2 * MARGIN)
            if size < 40:
                warn(f"{teacher!r}: teacher name shrunk to {size:.0f}pt on the cover page")
            _text(t, MARGIN, 82, teacher, size, "name", mc.NAME_COLOR)
            _text(t, MARGIN, 106, f"Beverly Cleary Reading Challenge  ·  {month}  ·  "
                                  f"{len(results)} {'student' if len(results) == 1 else 'students'}", 13, color=GREY)
            return 128.0
        _text(t, MARGIN, 62, f"{teacher} (continued)", 20, "name", mc.NAME_COLOR)
        return 90.0

    y = new_page(True)

    tally = prize_tally(results)
    if tally:
        text = ", ".join(f"{n} × {item}" for item, n in tally)
        _text(t, MARGIN, y, "Prizes to bag for this class", 12)
        y += 17
        for line in _wrap_items(t, text.split(", "), PAGE_W - 2 * MARGIN, SIZE):
            _text(t, MARGIN, y, line, SIZE)
            y += LINE_H
    else:
        _text(t, MARGIN, y, "No prizes this month for this class.", 12)
        y += 17
    y += 10

    def header(y):
        _text(t, COL_NAME, y, "Student", 10, color=GREY)
        _right(t, COL_MONTH, y, "This month", 10, GREY)
        _right(t, COL_TOTAL, y, "Total", 10, GREY)
        _text(t, COL_PRIZE, y, "Prizes", 10, color=GREY)
        page.draw_line((MARGIN, y + 5), (PAGE_W - MARGIN, y + 5), color=RULE, width=0.8)
        return y + 5 + 4

    y = header(y)
    for r in results:
        s = r.student
        items = [p[2] for p in r.prizes]
        lines = _wrap_items(t, items, PRIZE_W, SIZE) if items else ["—"]
        name_lines, name_size = _wrap_name(t, s.name, NAME_W, SIZE)
        row_h = max(len(lines), len(name_lines)) * LINE_H + ROW_PAD
        if y + row_h > BOTTOM:
            y = new_page(False)
            y = header(y)
        base = y + LINE_H
        page.draw_rect(pymupdf.Rect(COL_BOX, base - 9, COL_BOX + 10, base + 1), color=mc.BLACK, width=0.8)
        if name_size < SIZE:
            warn(f"{s.name!r}: name shrunk on the cover page")
        elif len(name_lines) > 1:
            warn(f"{s.name!r}: long name wrapped onto {len(name_lines)} lines on the cover page")
        for i, ln in enumerate(name_lines):
            _text(t, COL_NAME, base + i * LINE_H, ln, name_size)
        _right(t, COL_MONTH, base, f"{r.pages:,}")
        _right(t, COL_TOTAL, base, f"{s.total:,}")
        for i, line in enumerate(lines):
            _text(t, COL_PRIZE, base + i * LINE_H, line, SIZE, color=mc.BLACK if items else GREY)
        y += row_h
        page.draw_line((MARGIN, y - 1), (PAGE_W - MARGIN, y - 1), color=(0.92, 0.92, 0.92), width=0.5)
    return pages_added


def render_print_pdf(results, month, out_path, table=None) -> list[str]:
    """Write the classroom-grouped print PDF. Returns warnings (shrunk or wrapped text, etc.).

    `results` come from checkpoint.apply_month. `month` is anything checkpoint.month_key accepts."""
    table = table or mc.load_levels()
    label = ck.month_label(ck.month_key(month))
    warnings: list[str] = []
    doc = pymupdf.open()
    toc = []
    for teacher, rs in ck.classrooms(results):
        start = doc.page_count + 1
        draw_cover_pages(doc, teacher, rs, label, warnings.append)
        for r in rs:
            row = {"name": r.student.name, "pages": str(r.pages), "total_pages": str(r.student.total),
                   "_default_month": label}
            levels = [p[1] for p in r.prizes]
            mc.draw_certificate(doc, row, levels, warnings.append, table)
        toc.append([1, f"{teacher} ({len(rs)})", start])
    if toc:
        doc.set_toc(toc)
    doc.subset_fonts()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()
    return warnings
