"""The checkpoint: who is in the challenge, their running totals, and which prizes are accounted for.

One checkpoint is the state *after* a month has been processed. Processing the next month reads
it, applies that month's page counts, and writes the next checkpoint.

Rules
  * `total` is the running total of pages after the last processed month.
  * Prizes are the levels this month's change in total crosses: previous < threshold <= new total.
    That is exactly what the certificate shows, and nothing is carried over: a child with no sheet
    one month gets nothing for it, and a total corrected by hand does not trigger prizes (hand the
    prize over yourself if a child reports a mistake).
  * A month can only be applied once, and only after the previous one.
  * Every student needs a teacher: a student without one cannot be put in a classroom.
  * Students never get deleted. A student who leaves is marked `left` so history stays intact.

On disk it is an .xlsx so people can open it. See `save` / `load` for the sheets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

FORMAT_VERSION = 1
ACTIVE, LEFT = "active", "left"


class CheckpointError(ValueError):
    """Something about the checkpoint or the month being applied is wrong; message is user-readable."""


def to_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(re.sub(r"[,\s]", "", str(value)))


def month_key(text: str) -> str:
    """'October 2026' or '2026-10' -> '2026-10'."""
    text = str(text).strip()
    for fmt in ("%Y-%m", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            pass
    raise CheckpointError(f"cannot read month {text!r}; use 'October 2026' or '2026-10'")


def month_label(key: str) -> str:
    return datetime.strptime(key, "%Y-%m").strftime("%B %Y")


@dataclass(frozen=True)
class Student:
    id: str
    name: str
    teacher: str
    grade: str = ""
    total: int = 0
    status: str = ACTIVE
    notes: str = ""


@dataclass(frozen=True)
class HistoryRow:
    month: str      # '2026-10'
    student_id: str
    pages: int
    total_after: int
    source: str = ""  # e.g. "scan p. 12" or "seed"


@dataclass
class Checkpoint:
    month: str | None = None          # last month applied; None for a seed that has not had one yet
    students: dict[str, Student] = field(default_factory=dict)
    history: list[HistoryRow] = field(default_factory=list)

    def copy(self) -> "Checkpoint":
        return Checkpoint(self.month, dict(self.students), list(self.history))


# --------------------------------------------------------------------------- students
def next_id(cp: Checkpoint) -> str:
    nums = [int(m.group(1)) for sid in cp.students if (m := re.fullmatch(r"S(\d+)", sid))]
    return f"S{(max(nums) + 1 if nums else 1):04d}"


def add_student(cp: Checkpoint, name: str, teacher: str, grade: str = "", total: int = 0,
                notes: str = "") -> Student:
    """Add a student (mutates `cp`). A teacher is required. A child joining with an existing `total`
    gets no prizes for it: only pages added from now on can cross a level."""
    name, teacher = name.strip(), teacher.strip()
    if not name:
        raise CheckpointError("a student needs a name")
    if not teacher:
        raise CheckpointError(f"{name}: a student needs a classroom teacher")
    if total < 0:
        raise CheckpointError(f"{name}: total cannot be negative")
    s = Student(next_id(cp), name, teacher, str(grade).strip(), int(total), ACTIVE, notes)
    cp.students[s.id] = s
    return s


def find_by_name(cp: Checkpoint, name: str, include_left: bool = False) -> list[Student]:
    """Exact (case-insensitive) name matches. Fuzzy matching lives elsewhere."""
    key = " ".join(name.lower().split())
    return [s for s in cp.students.values()
            if " ".join(s.name.lower().split()) == key and (include_left or s.status == ACTIVE)]


def validate(cp: Checkpoint, require_teacher: bool = True) -> list[str]:
    """Problems that should be fixed before a checkpoint is used. Empty list means fine."""
    problems = []
    seen = {}
    for s in cp.students.values():
        if require_teacher and not s.teacher.strip():
            problems.append(f"{s.id} {s.name}: no teacher")
        if s.status not in (ACTIVE, LEFT):
            problems.append(f"{s.id} {s.name}: status {s.status!r} is not active/left")
        if s.total < 0:
            problems.append(f"{s.id} {s.name}: negative total")
        if s.status == ACTIVE:
            key = (" ".join(s.name.lower().split()), s.teacher.lower())
            if key in seen:
                problems.append(f"{s.id} and {seen[key]} are both active as {s.name!r} with {s.teacher}: duplicate?")
            seen[key] = s.id
    return problems


# --------------------------------------------------------------------------- applying a month
@dataclass(frozen=True)
class Entry:
    """One child's reading for the month, after every question about the scan was resolved."""
    student_id: str
    pages: int
    source: str = ""   # scan page reference, kept in History


@dataclass(frozen=True)
class Result:
    """What the printout needs for one child."""
    student: Student                   # state AFTER the month
    pages: int
    previous_total: int
    prizes: tuple                      # ((threshold, label, prize), ...) newly given, ascending
    source: str = ""


def prizes_crossed(previous: int, total: int, table) -> tuple:
    """((threshold, label, prize), ...) for every level with  previous < threshold <= total."""
    return tuple((t, label, prize) for t, label, prize in table if previous < t <= total)


def apply_month(cp: Checkpoint, month: str, entries: list[Entry], table) -> tuple[Checkpoint, list[Result]]:
    """Return (new checkpoint, results sorted by teacher then name). `cp` is not modified.

    Children with no entry are carried forward unchanged (and keep anything they are still owed)."""
    key = month_key(month)
    if cp.month is not None and key <= cp.month:
        raise CheckpointError(
            f"{month_label(key)} cannot be applied: the checkpoint already includes {month_label(cp.month)}")
    problems, seen = [], set()
    for e in entries:
        if e.student_id not in cp.students:
            problems.append(f"entry {e.source or e.student_id}: no such student {e.student_id}")
        elif cp.students[e.student_id].status != ACTIVE:
            problems.append(f"entry {e.source or e.student_id}: {cp.students[e.student_id].name} is marked left")
        if e.student_id in seen:
            who = cp.students[e.student_id].name if e.student_id in cp.students else e.student_id
            problems.append(f"{who}: two entries this month (merge them or void one)")
        seen.add(e.student_id)
        if e.pages < 0:
            problems.append(f"entry {e.source or e.student_id}: negative pages")
        if e.student_id in cp.students and not cp.students[e.student_id].teacher.strip():
            problems.append(f"{cp.students[e.student_id].name}: no teacher, so no classroom to put them in")
    # Students with no sheet this month may still lack a teacher; they are carried forward and reported.
    problems += validate(cp, require_teacher=False)
    if problems:
        raise CheckpointError("; ".join(problems))

    new = cp.copy()
    new.month = key
    results = []
    for e in entries:
        old = cp.students[e.student_id]
        total = old.total + e.pages
        prizes = prizes_crossed(old.total, total, table)
        updated = replace(old, total=total)
        new.students[old.id] = updated
        new.history.append(HistoryRow(key, old.id, e.pages, total, e.source))
        results.append(Result(updated, e.pages, old.total, prizes, e.source))
    results.sort(key=lambda r: (r.student.teacher.lower(), r.student.name.lower()))
    return new, results


def classrooms(results: list[Result]) -> list[tuple[str, list[Result]]]:
    """Group results by teacher, teachers in alphabetical order, children alphabetical within."""
    groups: dict[str, list[Result]] = {}
    for r in results:
        groups.setdefault(r.student.teacher, []).append(r)
    return sorted(((t, sorted(rs, key=lambda r: r.student.name.lower())) for t, rs in groups.items()),
                  key=lambda g: g[0].lower())


def not_seen(cp: Checkpoint, results: list[Result]) -> list[Student]:
    """Active students with no entry this month (worth a look: lost sheet, or just absent)."""
    done = {r.student.id for r in results}
    return sorted((s for s in cp.students.values() if s.status == ACTIVE and s.id not in done),
                  key=lambda s: (s.teacher.lower(), s.name.lower()))


# --------------------------------------------------------------------------- xlsx
STUDENT_COLS = ["ID", "Name", "Teacher", "Grade", "Total pages", "Status", "Notes"]
HISTORY_COLS = ["Month", "ID", "Pages", "Total after", "Source"]


def save(cp: Checkpoint, path, results: list[Result] | None = None, table=None) -> None:
    """Write the checkpoint workbook.

    Sheets: Students and History are the data (read back by `load`). 'Last month' and 'Meta'
    are for people; `load` ignores 'Last month'. Pass `results` to fill 'Last month'."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Students"
    ws.append(STUDENT_COLS)
    for s in sorted(cp.students.values(), key=lambda s: (s.teacher.lower(), s.name.lower())):
        ws.append([s.id, s.name, s.teacher, s.grade, s.total, s.status, s.notes])

    lm = wb.create_sheet("Last month")
    lm.append(["Month", "Teacher", "Name", "Pages this month", "Total pages", "Prizes given", "Source"])
    for r in results or []:
        lm.append([month_label(cp.month) if cp.month else "", r.student.teacher, r.student.name, r.pages,
                   r.student.total, "; ".join(f"{p[1]}: {p[2]}" for p in r.prizes), r.source])

    hs = wb.create_sheet("History")
    hs.append(HISTORY_COLS)
    for h in cp.history:
        hs.append([h.month, h.student_id, h.pages, h.total_after, h.source])

    meta = wb.create_sheet("Meta")
    meta.append(["Key", "Value"])
    meta.append(["format_version", FORMAT_VERSION])
    meta.append(["month", cp.month or ""])
    meta.append(["note", "Edit Students freely (fix a name, teacher or total). Do not rename the sheets or columns."])

    for sheet, widths in ((ws, [8, 26, 18, 8, 12, 9, 40]), (lm, [14, 18, 26, 14, 12, 50, 14]),
                          (hs, [10, 8, 8, 12, 16]), (meta, [16, 70])):
        for c in sheet[1]:
            c.font = Font(bold=True)
        sheet.freeze_panes = "A2"
        for i, w in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(i)].width = w
    for sheet, cols in ((ws, "E"), (lm, "DE"), (hs, "CD")):
        for col in cols:
            for cell in sheet[col][1:]:
                cell.number_format = "#,##0"
    wb.save(path)


def _sheet_rows(wb, name, columns, path):
    if name not in wb.sheetnames:
        raise CheckpointError(f"{path}: no sheet called {name!r}")
    rows = list(wb[name].iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in (rows[0] if rows else [])]
    missing = [c for c in columns if c not in header]
    if missing:
        raise CheckpointError(f"{path}: sheet {name!r} is missing column(s) {', '.join(missing)}")
    idx = {c: header.index(c) for c in columns}
    for row in rows[1:]:
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        yield {c: row[i] if i < len(row) else None for c, i in idx.items()}


def _text(v) -> str:
    return "" if v is None else str(v).strip()


def load(path) -> Checkpoint:
    from openpyxl import load_workbook

    path = Path(path)
    wb = load_workbook(path, data_only=True)
    cp = Checkpoint()
    for sheet, columns in (("Students", STUDENT_COLS), ("History", HISTORY_COLS)):
        _ = list(_sheet_rows(wb, sheet, columns, path))  # header check with a clear message
    for r in _sheet_rows(wb, "Students", STUDENT_COLS, path):
        sid = _text(r["ID"])
        if not sid:
            raise CheckpointError(f"{path}: a student row has no ID")
        if sid in cp.students:
            raise CheckpointError(f"{path}: ID {sid} appears twice")
        try:
            total = to_int(r["Total pages"] or 0)
        except ValueError:
            raise CheckpointError(f"{path}: {sid} has a total that is not a number") from None
        cp.students[sid] = Student(sid, _text(r["Name"]), _text(r["Teacher"]), _text(r["Grade"]), total,
                                   _text(r["Status"]).lower() or ACTIVE, _text(r["Notes"]))
    for r in _sheet_rows(wb, "History", HISTORY_COLS, path):
        cp.history.append(HistoryRow(month_key(r["Month"]), _text(r["ID"]), to_int(r["Pages"] or 0),
                                     to_int(r["Total after"] or 0), _text(r["Source"])))
    meta = {_text(a): b for a, b in ((row[0], row[1]) for row in wb["Meta"].iter_rows(min_row=2, values_only=True))
            if a} if "Meta" in wb.sheetnames else {}
    version = meta.get("format_version")
    if version is not None and to_int(version) > FORMAT_VERSION:
        raise CheckpointError(f"{path}: written by a newer version of this program (format {version})")
    cp.month = month_key(meta["month"]) if _text(meta.get("month")) else None
    return cp
