"""Turn what was read from the scans into one clean entry per child, with a person settling every doubt.

Inputs
  records    what extraction read from each sheet (SheetRecord), one per scanned sheet
  decisions  what the person decided, saved as work/decisions.json so a review can stop and resume
  checkpoint last month's students (plus optional hints from last year)

`plan()` is pure: it shows what is settled and what still needs a person (Issue). `finalize()` only
works when nothing is open; it adds any new students (each with a teacher) and returns the entries to
apply. Nothing is ever settled silently except an exact name match with every field read confidently.

Decisions, keyed by sheet ("<file>#<page>"):
    {"student": "S0003"}                          this sheet is that student
    {"teacher": "Moren"}                          teacher for an existing student who has none on file
    {"new_student": {"name":..., "teacher":..., "grade":..., "total": 0}}   add a student
    {"skip": "reason"}                            ignore this sheet
    {"pages": 120}                                use this page count (overrides the read one)
    {"pages_ok": true}                            the read page count is right (for low confidence)
    {"month_ok": true}                            sheet month differs from the batch but count it anyway
    {"duplicate": "sum" | "skip"}                 a second sheet for a child already seen this month
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from rc import checkpoint as ck
from rc import matching as mt
from rc import workspace as wsp

HIGH, LOW, UNREADABLE = "high", "low", "unreadable"


@dataclass(frozen=True)
class SheetRecord:
    file: str
    page: int
    name: str = ""
    teacher: str = ""
    month: str = ""            # as written on the sheet; "" if none
    pages: int | None = None   # this month's pages (the child's own written total)
    confidence: str = HIGH     # worst confidence among name / teacher / pages
    pages_confidence: str = HIGH
    note: str = ""             # anything extraction wants a person to see ("two totals: 158 vs 2300")

    @property
    def key(self) -> str:
        return f"{self.file}#{self.page}"

    @property
    def label(self) -> str:
        return f"{self.file} p. {self.page}"


@dataclass(frozen=True)
class Issue:
    key: str
    kind: str                  # who | teacher | pages | month | duplicate
    message: str
    candidates: tuple = ()     # matching.Candidate for 'who'; teacher names for 'teacher'


@dataclass
class Settled:
    record: SheetRecord
    student_id: str | None = None          # existing student
    set_teacher: str = ""                  # teacher to record for an existing student who had none
    new_student: dict | None = None        # to be added
    pages: int | None = None
    skipped: str = ""
    merged_from: list = field(default_factory=list)   # extra sheets summed into this one


@dataclass
class Plan:
    settled: list[Settled]
    issues: list[Issue]
    matches: dict[str, mt.Match]

    @property
    def ready(self) -> bool:
        return not self.issues


# ------------------------------------------------------------------ decisions on disk
def load_decisions(path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ck.CheckpointError(f"{path}: decisions file is damaged ({e})") from None


def save_decisions(decisions: dict, path) -> None:
    wsp.write_atomic(path, json.dumps(decisions, indent=2, sort_keys=True, ensure_ascii=False))


def load_records(path) -> list[SheetRecord]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [SheetRecord(**r) for r in rows]


def save_records(records: list[SheetRecord], path) -> None:
    wsp.write_atomic(path, json.dumps([r.__dict__ for r in records], indent=2, ensure_ascii=False))


# ------------------------------------------------------------------ planning
MONTH_NAMES = {name.lower(): i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}


def sheet_month_matches(written: str, batch: str) -> bool:
    """Does what a child wrote for the month ('October', 'Oct.', '10/5', '9/19/26', 'October 2026') fit the batch?

    Only the month, and the year when one is written, are compared. Anything unreadable does not match."""
    want_year, want_month = (int(x) for x in ck.month_key(batch).split("-"))
    text = " ".join(written.lower().replace(",", " ").replace(".", " ").split())
    try:
        got = ck.month_key(text)                              # '2026-10', 'october 2026'
        return got == ck.month_key(batch)
    except ck.CheckpointError:
        pass
    m = re.fullmatch(r"([a-z]+)(?: (\d{4}|\d{2}))?", text)
    if m:
        month = next((n for name, n in MONTH_NAMES.items() if name.startswith(m.group(1)) and len(m.group(1)) >= 3),
                     None)
        if month is None:
            return False
        year = m.group(2)
        return month == want_month and (year is None or (2000 + int(year) if len(year) == 2 else int(year)) == want_year)
    m = re.fullmatch(r"(\d{1,2})\s*[/\-]\s*(\d{1,2})(?:\s*[/\-]\s*(\d{4}|\d{2}))?", text)   # M/D or M/D/Y (US order)
    if m:
        year = m.group(3)
        return int(m.group(1)) == want_month and (
            year is None or (2000 + int(year) if len(year) == 2 else int(year)) == want_year)
    return False


def _identity(s: Settled, cp: ck.Checkpoint) -> str:
    if s.student_id:
        return s.student_id
    n = s.new_student
    return "new:" + mt.normalize(n["name"]) + "|" + mt.normalize(n.get("teacher", ""))


def plan(records: list[SheetRecord], cp: ck.Checkpoint, batch_month: str, decisions: dict,
         hints: ck.Checkpoint | None = None) -> Plan:
    batch = ck.month_key(batch_month)
    settled, issues, matches = [], [], {}
    teachers = sorted({s.teacher for s in cp.students.values() if s.teacher})

    for rec in records:
        d = decisions.get(rec.key, {})
        if d.get("skip"):
            settled.append(Settled(rec, skipped=str(d["skip"])))
            continue
        item = Settled(rec)

        # who is this?
        m = mt.match(rec.name, rec.teacher, cp, hints)
        matches[rec.key] = m
        if "student" in d:
            if d["student"] not in cp.students:
                issues.append(Issue(rec.key, "who", f"{rec.label}: decision names unknown student {d['student']}",
                                    m.candidates))
            else:
                item.student_id = d["student"]
        elif "new_student" in d:
            n = dict(d["new_student"])
            n["name"] = (n.get("name") or rec.name).strip()
            n["teacher"] = (n.get("teacher") or "").strip()
            item.new_student = n
            if not n["name"]:
                issues.append(Issue(rec.key, "who", f"{rec.label}: a new student needs a name"))
            elif not n["teacher"]:
                issues.append(Issue(rec.key, "teacher",
                                    f"{rec.label}: {n['name']} is new and needs a classroom teacher",
                                    tuple(teachers)))
        elif m.status == mt.EXACT and rec.confidence == HIGH:
            item.student_id = m.best.student.id
        else:
            why = {mt.EXACT: "name read with low confidence", mt.LIKELY: "probably", mt.AMBIGUOUS: "could be several",
                   mt.HINT: "not on this year's roster (was on last year's)", mt.NONE: "no one by that name"}[m.status]
            issues.append(Issue(rec.key, "who", f"{rec.label}: {rec.name or '(no name)'} / {rec.teacher or '(no teacher)'}"
                                                f" - {why}", m.candidates))

        # a classroom is needed for the certificate: the operator has to look it up (no automated source)
        if item.student_id and not cp.students[item.student_id].teacher.strip():
            if d.get("teacher", "").strip():
                item.set_teacher = d["teacher"].strip()
            else:
                issues.append(Issue(rec.key, "teacher",
                                    f"{rec.label}: {cp.students[item.student_id].name} has no teacher on file; "
                                    f"look it up and enter it", tuple(teachers)))

        # how many pages?
        if "pages" in d:
            item.pages = int(d["pages"])
        elif rec.pages is None or rec.pages_confidence == UNREADABLE:
            issues.append(Issue(rec.key, "pages", f"{rec.label}: page count could not be read" +
                                (f" ({rec.note})" if rec.note else "")))
        elif rec.pages_confidence == LOW or rec.note:
            if d.get("pages_ok"):
                item.pages = rec.pages
            else:
                issues.append(Issue(rec.key, "pages", f"{rec.label}: read {rec.pages:,} pages, not sure" +
                                    (f" ({rec.note})" if rec.note else "")))
        else:
            item.pages = rec.pages
        if item.pages is not None and item.pages < 0:
            issues.append(Issue(rec.key, "pages", f"{rec.label}: pages cannot be negative"))

        # the right month?
        if rec.month and not d.get("month_ok") and not sheet_month_matches(rec.month, batch):
            issues.append(Issue(rec.key, "month", f"{rec.label}: sheet says {rec.month!r}, batch is "
                                                  f"{ck.month_label(batch)}"))
        settled.append(item)

    # two sheets for the same child
    first: dict[str, Settled] = {}
    for item in settled:
        if item.skipped or (item.student_id is None and item.new_student is None):
            continue
        ident = _identity(item, cp)
        if ident not in first:
            first[ident] = item
            continue
        d = decisions.get(item.record.key, {})
        choice = d.get("duplicate")
        if choice == "sum":
            if item.pages is not None and first[ident].pages is not None:
                first[ident].pages += item.pages
                first[ident].merged_from.append(item.record)
            item.skipped = "summed into " + first[ident].record.label
        elif choice == "skip":
            item.skipped = "duplicate of " + first[ident].record.label
        else:
            who = cp.students[item.student_id].name if item.student_id else item.new_student["name"]
            issues.append(Issue(item.record.key, "duplicate",
                                f"{item.record.label}: {who} already has {first[ident].record.label} this month "
                                f"({first[ident].pages} vs {item.pages} pages): add them together, or skip this one?"))
    return Plan(settled, issues, matches)


# ------------------------------------------------------------------ finishing
def finalize(p: Plan, cp: ck.Checkpoint, table=None) -> tuple[ck.Checkpoint, list[ck.Entry], list[str]]:
    """(checkpoint with new students added, entries, summary lines). Raises while anything is open."""
    if p.issues:
        raise ck.CheckpointError(f"{len(p.issues)} question(s) still open; first: {p.issues[0].message}")
    cp = cp.copy()
    entries, notes, created = [], [], {}
    for item in p.settled:
        if item.student_id and item.set_teacher and not item.skipped:
            old = cp.students[item.student_id]
            cp.students[old.id] = ck.replace(old, teacher=item.set_teacher)
            notes.append(f"teacher for {old.name} set to {item.set_teacher} ({item.record.label})")
    for item in p.settled:
        rec = item.record
        if item.skipped:
            notes.append(f"skipped {rec.label} ({rec.name or 'no name'}): {item.skipped}")
            continue
        sid = item.student_id
        if sid is None:
            n = item.new_student
            ident = _identity(item, cp)
            if ident not in created:
                s = ck.add_student(cp, n["name"], n["teacher"], str(n.get("grade", "")), int(n.get("total", 0) or 0),
                                   notes=f"added {rec.label}")
                created[ident] = s.id
                notes.append(f"new student {s.name} ({s.teacher}) from {rec.label}")
            sid = created[ident]
        source = ", ".join([rec.label] + [r.label for r in item.merged_from])
        entries.append(ck.Entry(sid, item.pages, source))
    return cp, entries, notes
