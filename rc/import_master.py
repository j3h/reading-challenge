"""One-time import: the master spreadsheet, printed to PDF, becomes the first checkpoint.

Only the first group of printed columns is used: name | teacher | 25/26 grade | previous total.
(The sheet's prize "x" columns and prize-stock table are ignored on purpose: prizes come from how far
each month's pages move a child's total, so the marks are not needed.) It reads the PDF's text layer,
so nothing is OCR'd.

    python -m rc.import_master master.pdf --out work/checkpoint_seed.xlsx
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf

from rc import checkpoint as ck

Y_TOLERANCE = 2.0
RED = 0xFF0000


class ImportProblem(ck.CheckpointError):
    pass


def _cells(page):
    """Horizontal text cells as (y, x0, x1, text, colour)."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if tuple(round(v) for v in line["dir"]) != (1, 0):
                continue
            for span in line["spans"]:
                text = span["text"].strip()
                if text:
                    out.append((line["bbox"][1], span["bbox"][0], span["bbox"][2], text, span["color"]))
    return out


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


def _is_details_page(cells):
    return any(t == "Teacher" and 200 <= x0 < 300 for _, x0, _, t, _ in cells)


def _student_rows(cells):
    """[(name, teacher, grade, total_text, was_red)] for one details page, stopping at the prize inventory."""
    out = []
    for _, row in _rows(cells):
        by = defaultdict(list)
        for _, x0, x1, text, colour in row:
            by[_column(x0, x1)].append((text, colour))
        name = " ".join(t for t, _ in by.get("name", []))
        teacher_cell = by["teacher"][0][0] if by.get("teacher") else ""
        if name == "Prize" or teacher_cell == "Level":     # start of the prize inventory table
            break
        if not name or name == "Teacher" or teacher_cell == "Teacher":
            continue
        out.append((" ".join(name.split()), teacher_cell, by["grade"][0][0] if by.get("grade") else "",
                    by["total"][0][0] if by.get("total") else "",
                    any(c == RED for vals in by.values() for _, c in vals)))
    return out


def parse_master(pdf_path, teacher_aliases=None):
    """Return (checkpoint, report lines). The checkpoint has month None.

    `teacher_aliases` maps a teacher as written in the master to the name to use ({'Biello/Tanner': 'Biello'}),
    e.g. when two teachers became one class."""
    aliases = {k.strip().lower(): v.strip() for k, v in (teacher_aliases or {}).items()}
    doc = pymupdf.open(pdf_path)
    cp = ck.Checkpoint()
    report, seen_names = [], Counter()
    for page in doc:
        cells = _cells(page)
        if not _is_details_page(cells):
            continue
        for name, teacher, grade, total_text, was_red in _student_rows(cells):
            teacher = aliases.get(teacher.strip().lower(), teacher)
            seen_names[name] += 1
            try:
                total = ck.to_int(total_text) if total_text else 0
            except ValueError:
                raise ImportProblem(f"{name!r} has a total I cannot read: {total_text!r}") from None
            s = ck.Student(ck.next_id(cp), name, teacher, grade, total,
                           notes="red in the master: no recent sheets turned in" if was_red else "")
            cp.students[s.id] = s
            if not total_text:
                report.append(f"{name}: no previous total in the master; imported as 0")
            if not teacher:
                report.append(f"{name}: no teacher in the master (needs one before they can get a certificate)")
            if not grade:
                report.append(f"{name}: no grade in the master")
    if not cp.students:
        raise ImportProblem(f"{pdf_path}: found no student rows (looking for a 'Teacher' column header); "
                            f"is this the master sheet printed to PDF?")
    for name, c in seen_names.items():
        if c > 1:
            report.append(f"{name}: appears {c} times in the master")
    return cp, report


def questions_for_staff(cp, report) -> str:
    """A plain-text list to take to whoever keeps the master sheet (names are real: keep it out of git)."""
    none = [s for s in cp.students.values() if not s.teacher]
    teachers = Counter(s.teacher for s in cp.students.values() if s.teacher)
    out = ["Questions about the master sheet", "=" * 32, ""]
    out += [f"1. {len(none)} students have no teacher. Who are their teachers this year (or have they left)?"]
    out += [f"     {s.name} - grade {s.grade or '?'}" for s in sorted(none, key=lambda s: s.name)]
    out += ["", "2. Teacher names as they appear (should any be merged?):",
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
