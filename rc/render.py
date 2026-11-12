"""The print files. Finishing a month writes `render_print_file`: the class lists for bagging (all classes, as few
pages as possible), then the certificates two to a letter sheet, cut-and-stack by class. `render_print_pdf` below is
the older one-certificate-per-page layout with a cover page per class, kept for reference and its tests.

Older layout: for each classroom, a cover page (checklist for bagging prizes) then its certificates.

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


def prize_tally(results, owed_rows=()) -> list[tuple[str, int]]:
    """[(prize item, how many to bag)] in the order of the levels table (smallest prize first). `owed_rows`:
    [(student, [(threshold, label, prize)])] crossed in an earlier month but never handed out."""
    counts = Counter(p[2] for r in results for p in r.prizes)
    counts.update(p[2] for _, levels in owed_rows for p in levels)
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


def draw_cover_pages(doc, teacher, results, month, warn, owed_rows=()) -> int:
    """Append the cover page(s) for one classroom. Returns how many pages were added. Prizes owed from an earlier
    month are bagged too: added to the tally, marked "(owed)" by the child, with a row for an owed child who has no
    sheet this month."""
    owed = {s.id: levels for s, levels in owed_rows}
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

    tally = prize_tally(results, owed_rows)
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
    seen = {r.student.id for r in results}
    extra = [ck.Result(s, None, s.total, ()) for s, _ in owed_rows if s.id not in seen]
    for r in sorted(list(results) + extra, key=lambda r: r.student.name.lower()):
        s = r.student
        items = [p[2] for p in r.prizes] + [f"{p[2]} (owed)" for p in owed.get(s.id, [])]
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
        _right(t, COL_MONTH, base, f"{r.pages:,}" if r.pages is not None else "—", color=mc.BLACK if r.pages is not None else GREY)
        _right(t, COL_TOTAL, base, f"{s.total:,}")
        for i, line in enumerate(lines):
            _text(t, COL_PRIZE, base + i * LINE_H, line, SIZE, color=mc.BLACK if items else GREY)
        y += row_h
        page.draw_line((MARGIN, y - 1), (PAGE_W - MARGIN, y - 1), color=(0.92, 0.92, 0.92), width=0.5)
    return pages_added


def render_print_pdf(results, month, out_path, table=None, owed_rows=()) -> list[str]:
    """Write the classroom-grouped print PDF. Returns warnings (shrunk or wrapped text, etc.).

    `results` come from checkpoint.apply_month. `month` is anything checkpoint.month_key accepts. `owed_rows`:
    [(student, levels)] still owed from earlier months; a class with only owed prizes gets a cover page alone."""
    table = table or mc.load_levels()
    label = ck.month_label(ck.month_key(month))
    warnings: list[str] = []
    doc = pymupdf.open()
    toc = []
    classes = dict(ck.classrooms(results))
    for s, _ in owed_rows:
        classes.setdefault(s.teacher, [])
    for teacher in sorted(classes, key=str.lower):
        rs = classes[teacher]
        start = doc.page_count + 1
        draw_cover_pages(doc, teacher, rs, label, warnings.append, [o for o in owed_rows if o[0].teacher == teacher])
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


# ---------------------------------------------------------------------------- printing 2 per sheet, for a paper cutter
# Letter portrait, two certificates per sheet (top and bottom half), every sheet full. All the certificates are one
# run in class-list order (class after class, names in order), laid out "cut and stack": with S sheets, sheet i
# carries certificate i on top and S+i underneath, so cutting the stack across the middle (all at once, or a batch at
# a time from the top) and putting the bottom pile under the top pile gives them in list order. A small label in the
# side margin ("12 of 40 · Barry 3 of 6"), trimmed off with the edges, puts a dropped pile back together.
CUTTING = [
    "Cutting: take these list pages off first. Cut the certificate stack across the middle (the little marks at the edges).",
    "Too thick for the cutter? Cut a few sheets at a time from the top: top halves onto one pile, bottom halves onto another,",
    "each batch under the one before. Then put the bottom pile under the top pile: the certificates are now in the order of",
    "these lists, class after class (the grey number in the margin, e.g. 12 of 40, is each one's place).",
]
SHEET_W, SHEET_H = 612, 792
HALF_H = SHEET_H / 2
CERT_H = HALF_H - 36                      # 1/4 inch clear above and below each certificate (printers can't print to the edge)
CERT_W = CERT_H * PAGE_W / PAGE_H         # keeps the certificate's proportions
CERT_X = (SHEET_W - CERT_W) / 2


def cut_and_stack(n: int) -> list[tuple[int, int | None]]:
    """[(top, bottom)] certificate indexes per sheet for n certificates: one cut, bottom pile under top pile."""
    sheets = (n + 1) // 2
    return [(i, sheets + i if sheets + i < n else None) for i in range(sheets)]


def render_certificates_2up(results, month, out_path, table=None) -> list[str]:
    """Write certificates two to a letter sheet, class by class, laid out for a paper cutter. Returns warnings."""
    warnings: list[str] = []
    out, toc = _certificates_doc(results, month, table, warnings)
    _save(out, toc, out_path)
    return warnings


def _save(doc, toc, out_path):
    if toc:
        doc.set_toc(toc)
    doc.subset_fonts()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path), garbage=4, deflate=True)


def render_print_file(results, month, out_path, table=None, owed_rows=(), reprints=None) -> list[str]:
    """The one file to print: the class lists for bagging first (pull those pages off before cutting), then the
    certificates two to a sheet, every sheet full, cut and stack in class-list order. Returns warnings.

    `reprints`: {student id: (the month's pages so far, every level reached this month)} for a child whose certificate
    for this month was printed in an earlier round: theirs is printed again with the whole month (it replaces the
    earlier one), while the class list still bags only the prizes that are new."""
    warnings: list[str] = []
    doc = _class_lists_doc(results, month, owed_rows, warnings, reprints or {})
    lists = doc.page_count
    certs, toc = _certificates_doc(results, month, table, warnings, reprints or {})
    doc.insert_pdf(certs)
    _save(doc, [[1, "Class lists for bagging", 1]] + [[lv, title, page + lists] for lv, title, page in toc], out_path)
    return warnings


def _certificates_doc(results, month, table, warnings, reprints=None):
    reprints = reprints or {}
    table = table or mc.load_levels()
    label = ck.month_label(ck.month_key(month))
    singles = pymupdf.open()                       # each certificate on its own page first, exactly as designed
    meta = []                                      # (teacher, place in class, class size, printed again?)
    for teacher, rs in ck.classrooms(results):
        for k, r in enumerate(rs, 1):
            meta.append((teacher, k, len(rs), r.student.id in reprints))
        for r in rs:
            pages, levels = reprints.get(r.student.id, (r.pages, r.prizes))
            row = {"name": r.student.name, "pages": str(pages), "total_pages": str(r.student.total),
                   "_default_month": label}
            mc.draw_certificate(singles, row, [p[1] for p in levels], warnings.append, table)
    singles.subset_fonts()
    singles = pymupdf.open("pdf", singles.tobytes())   # placing pages needs a finished document
    out = pymupdf.open()
    n = len(meta)
    plan = cut_and_stack(n)
    sheet_of = {idx: i for i, pair in enumerate(plan) for idx in pair if idx is not None}
    toc, seen = [], set()
    for idx, (teacher, k, size, _) in enumerate(meta):         # each class: the sheet its first certificate is on
        if teacher not in seen:
            seen.add(teacher)
            toc.append([1, f"{teacher} ({size})", sheet_of[idx] + 1])
    for top, bottom in plan:
        sheet = out.new_page(width=SHEET_W, height=SHEET_H)
        for half, idx in ((0, top), (1, bottom)):
            if idx is None:
                continue
            teacher, k, size, again = meta[idx]
            y0 = half * HALF_H + (HALF_H - CERT_H) / 2
            sheet.show_pdf_page(pymupdf.Rect(CERT_X, y0, CERT_X + CERT_W, y0 + CERT_H), singles, idx)
            tag = (f"{idx + 1} of {n}  ·  {teacher} {k} of {size}  ·  {label}" +
                   ("  ·  REPLACES EARLIER" if again else ""))
            sheet.insert_text((CERT_X / 2 + 3, y0 + CERT_H / 2 + 75), tag, fontsize=7, color=GREY, rotate=90)
        for x0, x1 in ((18, 42), (SHEET_W - 42, SHEET_W - 18)):     # where to cut
            sheet.draw_line((x0, HALF_H), (x1, HALF_H), color=GREY, width=0.5)
    toc.sort(key=lambda e: e[2])
    return out, toc


# ---------------------------------------------------------------------------- the class lists, all together
LIST_MARGIN = 36
LIST_GAP = 18
LIST_COL_W = (SHEET_W - 2 * LIST_MARGIN - LIST_GAP) / 2
LIST_TOP, LIST_BOTTOM = 30, SHEET_H - 28
LIST_SIZE, LIST_LINE = 8.0, 9.1


def render_class_lists(results, month, out_path, owed_rows=()) -> list[str]:
    """Write the class lists alone (see `_class_lists_doc`). Returns warnings."""
    warnings: list[str] = []
    _save(_class_lists_doc(results, month, owed_rows, warnings), [], out_path)
    return warnings


def _class_lists_doc(results, month, owed_rows, warnings, reprints=None):
    reprints = reprints or {}
    """Every class's bagging list on as few letter pages as possible (two columns), with the whole school's prize
    count first, for pulling prizes from stock. Prizes owed from earlier months are included, marked "(owed)"."""
    label = ck.month_label(ck.month_key(month))
    doc = pymupdf.open()
    owed = {s.id: lv for s, lv in owed_rows}
    classes = dict(ck.classrooms(results))
    for s, _ in owed_rows:
        classes.setdefault(s.teacher, [])

    state = {"page": None, "t": None, "col": 0, "y": 0.0}

    def new_page():
        page = doc.new_page(width=SHEET_W, height=SHEET_H)
        state.update(page=page, t=mc.Typesetter(page, doc), col=0, y=LIST_TOP)

    def x0():
        return LIST_MARGIN + state["col"] * (LIST_COL_W + LIST_GAP)

    def need(h, continuing=False):
        """Make room for h points in the current column (next column, or next page). A class that carries on in
        the new column gets its teacher's name again."""
        if state["y"] + h <= LIST_BOTTOM:
            return
        if state["col"] == 0:
            state.update(col=1, y=state["top"])
        else:
            new_page()
            state["top"] = LIST_TOP
        if continuing and state.get("teacher"):
            _text(state["t"], x0(), state["y"] + 2, f"{state['teacher']} (continued)", 9, "name", mc.NAME_COLOR)
            state["y"] += 2 + LIST_LINE + 2

    new_page()
    t = state["t"]
    _text(t, LIST_MARGIN, LIST_TOP + 12, f"Reading Challenge  ·  {label}  ·  class lists for bagging", 13, "body")
    tally = prize_tally(results, owed_rows)
    y = LIST_TOP + 30
    lines = _wrap_items(t, [f"{n} × {item}" for item, n in tally], SHEET_W - 2 * LIST_MARGIN, 9) if tally else ["none"]
    _text(t, LIST_MARGIN, y, "Prizes to pull from stock, whole school:", 9, color=GREY)
    y += 12
    for line in lines:
        _text(t, LIST_MARGIN, y, line, 9)
        y += 11.5
    y += 3
    for line in CUTTING:
        _text(t, LIST_MARGIN, y, line, 7.5, color=GREY)
        y += 9.5
    state["page"].draw_line((LIST_MARGIN, y), (SHEET_W - LIST_MARGIN, y), color=RULE, width=0.8)
    state.update(y=y + 16, top=y + 16)

    for teacher in sorted(classes, key=str.lower):
        rs = classes[teacher]
        seen = {r.student.id for r in rs}
        rows = sorted(list(rs) + [ck.Result(s, None, s.total, ()) for s, _ in owed_rows
                                  if s.teacher == teacher and s.id not in seen], key=lambda r: r.student.name.lower())
        state["teacher"] = ""
        need(LIST_LINE * 3)                                     # a class heading never sits alone at a column foot
        state["teacher"] = teacher
        t, page = state["t"], state["page"]
        state["y"] += 2
        _text(t, x0(), state["y"], teacher, 10.5, "name", mc.NAME_COLOR)
        n_cert = len(rs)
        _right(t, x0() + LIST_COL_W, state["y"], f"{n_cert} certificate{'s' if n_cert != 1 else ''}", 8, GREY)
        state["y"] += 5
        page.draw_line((x0(), state["y"]), (x0() + LIST_COL_W, state["y"]), color=RULE, width=0.6)
        state["y"] += LIST_LINE
        for r in rows:
            s = r.student
            items = [p[2] for p in r.prizes] + [f"{p[2]} (owed)" for p in owed.get(s.id, [])]
            t = state["t"]
            name_lines, size = _wrap_name(t, s.name, LIST_COL_W - 70, LIST_SIZE)
            prize_lines = _wrap_items(t, items, LIST_COL_W - 14, LIST_SIZE - 0.5) if items else []
            h = (len(name_lines) + len(prize_lines)) * LIST_LINE + 2
            need(h, continuing=True)
            t, page, x = state["t"], state["page"], x0()
            y = state["y"]
            page.draw_rect(pymupdf.Rect(x, y - 7, x + 7, y), color=mc.BLACK, width=0.6)
            for i, ln in enumerate(name_lines):
                _text(t, x + 12, y + i * LIST_LINE, ln, size)
            again = reprints.get(s.id)
            _right(t, x + LIST_COL_W, y, (f"reprint, {again[0]:,} pp" if again else f"{r.pages:,} pp")
                   if r.pages is not None else "owed only", LIST_SIZE - 0.5, (0.75, 0.3, 0.05) if again else GREY)
            y += len(name_lines) * LIST_LINE
            for ln in prize_lines:
                _text(t, x + 12, y, ln, LIST_SIZE - 0.5, color=(0.15, 0.3, 0.55))
                y += LIST_LINE
            state["y"] = y + 2
        state["y"] += 5
    if doc.page_count > 1:
        warnings.append(f"class lists take {doc.page_count} pages")
    return doc
