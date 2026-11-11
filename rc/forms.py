"""Google Form responses (downloaded as CSV) as sheets for a month's review.

The response sheet keeps every submission ever made, so a month uses only the rows whose "Month" answer is
that month. Each kept row becomes a SheetRecord like a scanned page: typed text needs no reading (and costs
nothing), but it still goes through review, because the name is free text and a parent's form and the
child's paper sheet may be the same reading (the Add / Replace / Skip choice).

Columns are found by their headers, so a reworded question still works as long as it keeps its key word:
    timestamp | name | teacher (often "Grade and Teacher": "K- Hjorth", "3rd- Harris") | month | pages
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from rc import checkpoint as ck
from rc import review as rv

FORM_EXTS = {".csv"}


def _find(header, *words) -> int | None:
    h = [str(x or "").strip().lower() for x in header]
    for w in words:
        for i, x in enumerate(h):
            if w in x:
                return i
    return None


def split_grade_teacher(text: str) -> tuple[str, str]:
    """'K- Hjorth' -> ('K', 'Hjorth'); '3rd- Harris' -> ('3', 'Harris'); 'Ms. Barry' -> ('', 'Ms. Barry')."""
    text = " ".join(str(text or "").split())
    m = re.match(r"^(k|kinder\w*|\d+)(?:st|nd|rd|th)?\s*(?:grade)?\s*[-–,/:]\s*(.+)$", text, re.I)
    if m:
        grade = "K" if m.group(1).lower().startswith("k") else m.group(1)
        return grade, m.group(2).strip()
    return "", text


def _pages(text: str) -> int | None:
    s = str(text or "").replace(",", "").strip()
    return int(float(s)) if re.fullmatch(r"\d+(\.\d+)?", s) else None


def records(path, batch_month: str) -> tuple[list[rv.SheetRecord], list[str]]:
    """(sheets for `batch_month`, notes about rows left out). Row numbers are as in the sheet (header = 1)."""
    path = Path(path)
    want = ck.month_key(batch_month)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], [f"{path.name} is empty"]
    header = rows[0]
    col = {"time": _find(header, "timestamp", "time"), "name": _find(header, "name"),
           "teacher": _find(header, "teacher", "class"), "month": _find(header, "month"),
           "pages": _find(header, "pages", "how many")}
    missing = [k for k in ("name", "pages") if col[k] is None]
    if missing:
        raise ck.CheckpointError(f"{path.name}: no column for {', '.join(missing)} (headers: {', '.join(header)})")
    get = lambda r, k: (r[col[k]].strip() if col[k] is not None and col[k] < len(r) else "")
    out, skipped = [], []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        month = get(r, "month")
        if month and not rv.sheet_month_matches(month, want):
            continue                                       # another month's submission
        grade, teacher = split_grade_teacher(get(r, "teacher"))
        pages = _pages(get(r, "pages"))
        submitted = get(r, "time")
        if not month:
            skipped.append(f"{path.name} row {n} ({get(r, 'name') or 'no name'}): no month given; left out")
            continue
        out.append(rv.SheetRecord(
            path.name, n, get(r, "name"), teacher, month, pages,
            confidence=rv.HIGH, pages_confidence=rv.HIGH if pages is not None else rv.UNREADABLE,
            note=f"Google Form, submitted {submitted}" + ("" if pages is not None else
                                                           f"; pages answer was {get(r, 'pages')!r}"),
            grade=grade, submitted=submitted))
    return out, skipped
