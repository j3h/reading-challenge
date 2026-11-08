"""One-time import: the master spreadsheet, printed to PDF, becomes the first checkpoint.

The PDF is the sheet split into column groups, printed one after another:
  group 1  name | teacher | 25/26 grade | previous total        (then a prize inventory table we ignore)
  group 2  name | one "x" column per prize level, first pages .. 5000     (x = prize handed out)
  group 3  name | the same for 7,500 and up
Rows line up across the groups by their vertical position. The importer reads the text layer, so
nothing is OCR'd; it refuses to guess when the three groups disagree about who is on a row.

    python -m rc.import_master master.pdf --out work/checkpoint_seed.xlsx

Each child's `last_level` is the highest prize level with an x, so anything the child is owed but
was never marked as given shows up on their first certificate. Marks for a level that is not in
levels.csv are ignored (and reported).
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf

import make_certificates as mc
from rc import checkpoint as ck

Y_TOLERANCE = 2.0
RED = 0xFF0000


class ImportProblem(ck.CheckpointError):
    pass


def _lines(page):
    """Horizontal text cells as (y, x0, x1, text, colour), plus rotated header cells as (x0, text)."""
    cells, rotated = [], []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            horizontal = tuple(round(v) for v in line["dir"]) == (1, 0)
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:
                    continue
                if horizontal:
                    cells.append((line["bbox"][1], span["bbox"][0], span["bbox"][2], text, span["color"]))
                else:
                    rotated.append((line["bbox"][0], text))
    return cells, rotated


def _rows(cells):
    """Group cells into rows by y (within tolerance). Returns [(y, [cells sorted by x])]."""
    rows: list[list] = []
    for c in sorted(cells):
        if rows and abs(rows[-1][0][0] - c[0]) <= Y_TOLERANCE:
            rows[-1].append(c)
        else:
            rows.append([c])
    return [(r[0][0], sorted(r, key=lambda c: c[1])) for r in rows]


def _column(x0, x1):
    if x0 < 200:
        return "name"
    if x0 < 300:
        return "teacher"
    if x0 < 370:
        return "grade"
    return "total" if x1 < 445 else "current"


def _is_teacher_header_page(cells):
    return any(t == "Teacher" and 200 <= x0 < 300 for _, x0, _, t, _ in cells)


def _student_rows(page):
    """[(y, name, teacher, grade, total_text, was_red)] for one group-1 page, stopping at the inventory."""
    cells, _ = _lines(page)
    out = []
    for y, row in _rows(cells):
        by = defaultdict(list)
        for _, x0, x1, text, colour in row:
            by[_column(x0, x1)].append((text, colour))
        name = " ".join(t for t, _ in by.get("name", []))
        teacher_cell = by["teacher"][0][0] if by.get("teacher") else ""
        if name == "Prize" or teacher_cell == "Level":     # start of the prize inventory table
            break
        if not name or name == "Teacher" or teacher_cell == "Teacher":
            continue
        out.append((y, " ".join(name.split()), teacher_cell, by["grade"][0][0] if by.get("grade") else "",
                    by["total"][0][0] if by.get("total") else "",
                    any(c == RED for vals in by.values() for _, c in vals)))
    return out


def _header_thresholds(rotated, table):
    """[(x, threshold or None, text)] for one marks page, left to right, thresholds read from the headers."""
    known = {t for t, _, _ in table}
    out = []
    for x0, text in sorted(rotated):
        m = re.match(r"^\s*([\d,]+)\s*-", text)
        if re.search(r"first|^rst", text, re.I):
            out.append((x0, 1, text))
        elif m and ck.to_int(m.group(1)) in known:
            out.append((x0, ck.to_int(m.group(1)), text))
        else:   # a level that is not in levels.csv, or a clipped number (resolved by _fill_clipped)
            out.append((x0, None, text))
    return out


def _fill_clipped(headers, table):
    """Headers whose number was cut off by the page edge (e.g. '00 - $10 ...') get the one level that fits
    between their neighbours. Anything still unknown stays None, and its marks are ignored."""
    levels = sorted(t for t, _, _ in table)
    out = list(headers)
    for i, (x, thr, text) in enumerate(out):
        if thr is not None:
            continue
        looks_clipped = re.match(r"^\s*\d{1,3}\s*-", text) and not re.match(r"^\s*\d{1,3}(,\d{3})+", text)
        if not looks_clipped:
            continue
        lo = max((h[1] for h in out[:i] if h[1]), default=0)
        hi = min((h[1] for h in out[i + 1:] if h[1]), default=10**12)
        candidates = [t for t in levels if lo < t < hi]
        if len(candidates) == 1:
            out[i] = (x, candidates[0], text)
    return out


def _marks(page, table):
    """({y: {threshold: True}}, [problems]) from one marks page."""
    cells, rotated = _lines(page)
    headers = _fill_clipped(_header_thresholds(rotated, table), table)
    marks, notes = defaultdict(set), []
    for x, thr, text in headers:
        if thr is None:
            notes.append(f"column {text!r} is not a level in levels.csv: its marks are ignored")
    for y, x0, _, text, _ in cells:
        if text.lower() != "x" or x0 < 200 or y < 150:
            continue
        nearest = min(headers, key=lambda h: abs(h[0] - x0), default=None)
        if nearest is None or abs(nearest[0] - x0) > 8:
            notes.append(f"mark at y={y:.0f} has no column header")
            continue
        if nearest[1] is not None:
            marks[round(y)].add(nearest[1])
    return marks, notes


def _marks_near(marks, y):
    for dy in range(-int(Y_TOLERANCE), int(Y_TOLERANCE) + 1):
        if round(y) + dy in marks:
            return marks[round(y) + dy]
    return set()


def parse_master(pdf_path, table=None, teacher_aliases=None):
    """Return (checkpoint, report lines). The checkpoint has month None and last_level taken from the x marks.

    `teacher_aliases` maps a teacher as written in the master to the name to use ({'Biello/Tanner': 'Biello'}),
    e.g. when two teachers became one class."""
    table = table or mc.load_levels()
    aliases = {k.strip().lower(): v.strip() for k, v in (teacher_aliases or {}).items()}
    doc = pymupdf.open(pdf_path)
    cell_pages = [_lines(p)[0] for p in doc]
    group1 = [i for i, c in enumerate(cell_pages) if _is_teacher_header_page(c)]
    if not group1 or group1 != list(range(len(group1))) or doc.page_count != 3 * len(group1):
        raise ImportProblem(f"{pdf_path}: expected three equal groups of pages (details, early prize marks, "
                            f"late prize marks); got {doc.page_count} pages with detail headers on {group1}")
    n = len(group1)
    cp = ck.Checkpoint()
    report, mark_notes = [], []
    seen_names = Counter()
    for k in range(n):
        students = _student_rows(doc[k])
        early, notes_a = _marks(doc[k + n], table)
        late, notes_b = _marks(doc[k + 2 * n], table)
        mark_notes += notes_a + notes_b
        # the marks pages repeat the names: they must agree with the detail page
        for page_no in (k + n, k + 2 * n):
            names_there = {round(y): " ".join(t for _, x0, _, t, _ in row if x0 < 200)
                           for y, row in _rows(_lines(doc[page_no])[0])}
            for y, name, *_ in students:
                there = names_there.get(round(y)) or next(
                    (v for yy, v in names_there.items() if abs(yy - y) <= Y_TOLERANCE), None)
                if there is None or " ".join(there.split()) != name:
                    raise ImportProblem(f"page {page_no + 1}: row for {name!r} (detail page {k + 1}) shows "
                                        f"{there!r}; the page groups do not line up")
        for y, name, teacher, grade, total_text, was_red in students:
            teacher = aliases.get(teacher.strip().lower(), teacher)
            seen_names[name] += 1
            given = _marks_near(early, y) | _marks_near(late, y)
            try:
                total = ck.to_int(total_text) if total_text else 0
            except ValueError:
                raise ImportProblem(f"page {k + 1}: {name!r} has a total I cannot read: {total_text!r}") from None
            last = max(given, default=0)
            s = ck.Student(ck.next_id(cp), name, teacher, grade, total, last,
                           notes="teacher/grade were in red in the master" if was_red else "")
            cp.students[s.id] = s
            if not total_text:
                report.append(f"{name}: no previous total in the master; imported as 0")
            if not teacher:
                report.append(f"{name}: no teacher in the master (needs one before the first month is applied)")
            if not grade:
                report.append(f"{name}: no grade in the master")
            if last > total:
                report.append(f"{name}: marked as given up to {last:,} pages but the total is only {total:,}")
            elif ck.highest_level_at(total, table) > last:
                owed = [lbl for t, lbl, _ in table if last < t <= total]
                report.append(f"{name}: total {total:,} reaches {', '.join(owed)} but no x yet (will be on the "
                              f"first certificate)")
    for name, c in seen_names.items():
        if c > 1:
            report.append(f"{name}: appears {c} times in the master")
    report = sorted(set(mark_notes)) + report
    return cp, report


def questions_for_staff(cp, report) -> str:
    """A plain-text list to take to whoever keeps the master sheet (names are real: keep it out of git)."""
    red = [s for s in cp.students.values() if "red" in s.notes]
    none = [s for s in cp.students.values() if not s.teacher]
    marks = [r for r in report if "marked as given up to" in r]
    owed = [r for r in report if "no x yet" in r]
    teachers = Counter(s.teacher for s in cp.students.values() if s.teacher)
    out = ["Questions about the master sheet", "=" * 32, ""]
    out += [f"1. {len(red)} students have their teacher/grade in red. What does red mean (e.g. last year's class, "
            f"or total out of date)?"]
    out += [f"     {s.name} - {s.teacher or '(no teacher)'} - grade {s.grade or '?'} - {s.total:,} pages" for s in
            sorted(red, key=lambda s: s.name)]
    out += ["", f"2. {len(none)} students have no teacher. Who are their teachers this year?"]
    out += [f"     {s.name} - grade {s.grade or '?'}" for s in sorted(none, key=lambda s: s.name)]
    out += ["", f"3. {len(marks)} students are marked as given a prize beyond their total (total or marks wrong?):"]
    out += [f"     {r}" for r in marks]
    out += ["", f"4. {len(owed)} students passed a level with no x. Should they still be given that prize (no x = owed)?"]
    out += [f"     {r}" for r in owed]
    out += ["", "5. Teacher names as they appear (should any be merged, e.g. Biello and Biello/Tanner?):",
            "     " + ", ".join(f"{t} ({n})" for t, n in sorted(teachers.items()))]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", help="master spreadsheet printed to PDF")
    ap.add_argument("--out", required=True, help="checkpoint .xlsx to write (keep it out of git)")
    ap.add_argument("--teacher-alias", action="append", default=[], metavar="OLD=NEW",
                    help='use NEW wherever the master says OLD, e.g. "Biello/Tanner=Biello" (repeatable)')
    ap.add_argument("--questions", help="also write a plain-text list of questions about the master "
                                        "(for whoever keeps it); it contains real names, keep it out of git")
    args = ap.parse_args(argv)
    aliases = {}
    for a in args.teacher_alias:
        old, sep, new = a.partition("=")
        if not sep or not old.strip() or not new.strip():
            sys.exit(f"--teacher-alias needs OLD=NEW, got {a!r}")
        aliases[old] = new
    try:
        cp, report = parse_master(args.pdf, teacher_aliases=aliases)
    except (ck.CheckpointError, OSError, RuntimeError) as e:
        sys.exit(str(e))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    ck.save(cp, args.out)
    teachers = Counter(s.teacher or "(none)" for s in cp.students.values())
    print(f"Imported {len(cp.students)} students into {args.out}")
    print("Teachers:", ", ".join(f"{t} ({n})" for t, n in sorted(teachers.items())))
    if report:
        print(f"\n{len(report)} things to look at:")
        for line in report:
            print("  -", line)
    if args.questions:
        Path(args.questions).write_text(questions_for_staff(cp, report), encoding="utf-8")
        print(f"\nQuestions for staff written to {args.questions}")


if __name__ == "__main__":
    main()
