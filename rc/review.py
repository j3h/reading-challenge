"""Turn what was read from the scans into one clean entry per child, with a person settling every doubt.

Inputs
  records    what extraction read from each sheet (SheetRecord), one per scanned sheet
  decisions  what the person decided, saved as work/decisions.json so a review can stop and resume
  checkpoint last month's students (totals roll forward from year to year)

`plan()` is pure: it shows what is settled and what still needs a person (Issue). `finalize()` only
works when nothing is open; it adds any new students (each with a teacher) and returns the entries to
apply. Nothing is ever settled silently except an exact name match with every field read confidently.

Decisions, keyed by sheet ("<file>#<page>"):
    {"student": "S0003"}                          this sheet is that student
    {"remember_name": true}                       (with student) also match the name as written next time
    {"new_student": {"name":..., "teacher":..., "grade":..., "total": 0}}   add a student
    {"skip": "reason"}                            ignore this sheet
    {"pages": 120}                                use this page count (overrides the read one)
    {"pages_ok": true}                            the read page count is right (for low confidence)
    {"teacher": "Moren"}                          (with an existing student) their teacher now: fills a missing
                                                  teacher or records a class change
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
    boxes: dict = field(default_factory=dict)   # where each value was read: {"name": [x0, y0, x1, y1]} on 0-1000

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
    remember_name: bool = False            # keep the name written on the sheet as one of this child's names
    new_student: dict | None = None        # to be added
    pages: int | None = None
    skipped: str = ""
    merged_from: list = field(default_factory=list)   # extra sheets summed into this one
    warnings: list = field(default_factory=list)      # shown to the person, never block (e.g. month on the sheet)


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


def save_decisions(decisions: dict, path, source: str = "") -> None:
    """Write the decisions, and append every change to decisions.log next to it (one JSON line per sheet
    changed: time, sheet, before, after, where from), so nothing a person decided can vanish without a trace."""
    path = Path(path)
    before = load_decisions(path) if path.exists() else {}
    wsp.write_atomic(path, json.dumps(decisions, indent=2, sort_keys=True, ensure_ascii=False))
    from datetime import datetime
    stamp = datetime.now().isoformat(timespec="seconds")
    lines = [json.dumps({"time": stamp, "sheet": k, "before": before.get(k), "after": decisions.get(k), "source": source},
                        ensure_ascii=False)
             for k in sorted(set(before) | set(decisions)) if before.get(k) != decisions.get(k)]
    if lines:
        with open(path.with_name("decisions.log"), "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


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
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        year = None if year is None else (2000 + int(year) if len(year) == 2 else int(year))
        if month == want_month and year in (None, want_year):
            return True
        # handed in during the first days of the next month: that is still this month's total
        nxt_month, nxt_year = (1, want_year + 1) if want_month == 12 else (want_month + 1, want_year)
        return month == nxt_month and day <= 7 and year in (None, nxt_year)
    return False


def _identity(s: Settled, cp: ck.Checkpoint) -> str:
    if s.student_id:
        return s.student_id
    n = s.new_student
    return "new:" + mt.normalize(n["name"]) + "|" + mt.normalize(n.get("teacher", ""))


def plan(records: list[SheetRecord], cp: ck.Checkpoint, batch_month: str, decisions: dict) -> Plan:
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
        m = mt.match(rec.name, rec.teacher, cp)
        matches[rec.key] = m
        if "student" in d:
            if d["student"] not in cp.students:
                issues.append(Issue(rec.key, "who", f"{rec.label}: decision names unknown student {d['student']}",
                                    m.candidates))
            else:
                item.student_id = d["student"]
                item.remember_name = bool(d.get("remember_name"))
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
                   mt.NONE: "no one by that name"}[m.status]
            issues.append(Issue(rec.key, "who", f"{rec.label}: {rec.name or '(no name)'} / {rec.teacher or '(no teacher)'}"
                                                f" - {why}", m.candidates))

        # a classroom is needed for the certificate: the operator has to look it up (no automated source)
        if item.student_id and (d.get("teacher") or "").strip():
            if d["teacher"].strip() != cp.students[item.student_id].teacher:
                item.set_teacher = d["teacher"].strip()
        elif item.student_id and not cp.students[item.student_id].teacher.strip():
                issues.append(Issue(rec.key, "teacher",
                                    f"{rec.label}: {cp.students[item.student_id].name} has no teacher on file; "
                                    f"look it up and enter it", tuple(teachers)))

        # how many pages?
        if "pages" in d:
            item.pages = int(d["pages"])
        elif rec.pages is None or rec.pages_confidence == UNREADABLE:
            issues.append(Issue(rec.key, "pages", f"{rec.label}: page count could not be read" +
                                (f" ({rec.note})" if rec.note else "")))
        elif rec.pages_confidence == LOW:     # a note alone is shown to the person but never blocks
            if d.get("pages_ok"):
                item.pages = rec.pages
            else:
                issues.append(Issue(rec.key, "pages", f"{rec.label}: read {rec.pages:,} pages, not sure" +
                                    (f" ({rec.note})" if rec.note else "")))
        else:
            item.pages = rec.pages
        if item.pages is not None and item.pages < 0:
            issues.append(Issue(rec.key, "pages", f"{rec.label}: pages cannot be negative"))

        # the month on the sheet is often wrong or a date; the batch month is used, a mismatch is only shown
        if rec.month and not sheet_month_matches(rec.month, batch):
            item.warnings.append(f"sheet says {rec.month!r}; counted for {ck.month_label(batch)}")
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
                                f"({first[ident].pages} vs {item.pages} pages): add them together, or skip this one?",
                                (first[ident].pages, item.pages)))
    return Plan(settled, issues, matches)


# ------------------------------------------------------------------ finishing
def finalize(p: Plan, cp: ck.Checkpoint, table=None) -> tuple[ck.Checkpoint, list[ck.Entry], list[str]]:
    """(checkpoint with new students added, entries, summary lines). Raises while anything is open."""
    if p.issues:
        raise ck.CheckpointError(f"{len(p.issues)} question(s) still open; first: {p.issues[0].message}")
    cp = cp.copy()
    entries, notes, created = [], [], {}
    for item in p.settled:
        if item.student_id and item.remember_name and not item.skipped:
            old = cp.students[item.student_id]
            written = item.record.name.strip()
            known = {mt.normalize(old.name)} | {mt.normalize(n) for n in old.nickname_list}
            if written and mt.normalize(written) not in known:
                cp.students[old.id] = ck.replace(old, nicknames=", ".join(old.nickname_list + [written]))
                notes.append(f"{old.name} will also be recognised as {written!r} ({item.record.label})")
    for item in p.settled:
        if item.student_id and item.set_teacher and not item.skipped:
            old = cp.students[item.student_id]
            cp.students[old.id] = ck.replace(old, teacher=item.set_teacher)
            change = f"changed from {old.teacher} to" if old.teacher else "set to"
            notes.append(f"teacher for {old.name} {change} {item.set_teacher} ({item.record.label})")
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


# ------------------------------------------------------------------ suggestions
@dataclass
class Suggestion:
    """What the review screen pre-fills for a sheet that needs a look, and why it needs one."""
    decision: dict | None     # accepting this as-is settles the sheet; None if a person must fill something in
    reasons: list             # short, plain reasons the sheet was not settled automatically
    draft: dict = field(default_factory=dict)   # the same suggestion, possibly incomplete, for pre-filling a form


def teacher_guess(written: str, teachers) -> str:
    """The roster teacher the sheet's teacher most likely means ('Ms. Tracy' -> 'Tracy'), or ''."""
    return next((t for t in teachers if mt.teacher_agreement(written, t) == "agree"), "")


def suggest(p: Plan, cp: ck.Checkpoint, decisions: dict) -> dict[str, Suggestion]:
    """A suggested decision for every sheet that still has a question."""
    teachers = sorted({s.teacher for s in cp.students.values() if s.teacher})
    by_key: dict[str, list] = {}
    for i in p.issues:
        by_key.setdefault(i.key, []).append(i)
    out = {}
    for item in p.settled:
        rec, issues = item.record, by_key.get(item.record.key, [])
        if item.skipped or not issues:
            continue
        d = dict(decisions.get(rec.key, {}))
        reasons, complete = [], True
        m = p.matches.get(rec.key)
        guess = teacher_guess(rec.teacher, teachers)
        kinds = {i.kind for i in issues}
        if "student" in d or "new_student" in d:
            pass                                     # the person already chose who; keep it
        elif m and m.candidates:
            best = m.candidates[0]
            d["student"] = best.student.id
            d["remember_name"] = not best.reason.startswith(("same name", "same words"))
            others = [c for c in m.candidates[1:] if c.score >= best.score - 0.1]
            why = "name hard to read" if m.status == mt.EXACT else best.reason
            reasons.append(why + (f"; could also be {others[0].student.name}" if others else ""))
        else:
            d["new_student"] = {"name": rec.name, "teacher": guess, "grade": ""}
            reasons.append("not on the roster: add as a new student")
            if not guess:
                complete = False
                reasons.append(f"choose a teacher (sheet says {rec.teacher!r})" if rec.teacher else "choose a teacher")
            if not d["new_student"]["name"]:
                complete = False
                reasons.append("type the child's name")
        if "student" in d and d["student"] in cp.students and not cp.students[d["student"]].teacher and not d.get("teacher"):
            if guess:
                d["teacher"] = guess
                reasons.append(f"no teacher on file: using {guess} from the sheet")
            else:
                complete = False
                reasons.append("no teacher on file: choose one")
        if "pages" not in d:
            if rec.pages is None:
                complete = False
                reasons.append("type the pages: the total could not be read")
            else:
                d["pages"] = rec.pages
                if "pages" in kinds:
                    reasons.append(f"page count unsure" + (f": {rec.note}" if rec.note else ""))
        if "duplicate" in kinds and "duplicate" not in d:
            dup = next(i for i in issues if i.kind == "duplicate")
            first_pages, this_pages = (dup.candidates + (None, None))[:2]
            d["duplicate"] = "skip" if first_pages == this_pages else "sum"
            reasons.append("second sheet for this child: " + ("looks like the same sheet twice, skip it"
                                                                if d["duplicate"] == "skip" else "add the pages together"))
        out[rec.key] = Suggestion(d if complete else None, reasons or [i.message for i in issues], d)
    return out
