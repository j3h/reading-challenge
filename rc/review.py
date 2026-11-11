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
    {"student": "new@oct.pdf#3"}                  the child added as new from that sheet this month
    {"remember_name": true}                       (with student) also match the name as written next time
    {"new_student": {"name":..., "teacher":..., "grade":..., "total": 0}}   add a student
    {"skip": "reason"}                            ignore this sheet
    {"pages": 120}                                use this page count (overrides the read one)
    {"pages_ok": true}                            the read page count is right (for low confidence)
    {"teacher": "Moren"}                          (with an existing student) their teacher now: fills a missing
                                                  teacher or records a class change
    {"duplicate": "sum" | "replace" | "skip"}     another sheet for a child already seen this month (page order):
                                                  add its pages, use its pages instead, or ignore it

A child can hand in several sheets in one month. Their sheets are taken in page order: the first one starts
the month's count, and each later one says what to do with it, relative to the count so far.
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
    candidates: tuple = ()     # matching.Candidate for 'who'; teacher names for 'teacher'; (so far, this) for duplicate


@dataclass
class Settled:
    record: SheetRecord
    student_id: str | None = None          # existing student
    set_teacher: str = ""                  # teacher to record for an existing student who had none
    remember_name: bool = False            # keep the name written on the sheet as one of this child's names
    new_student: dict | None = None        # to be added
    pages: int | None = None
    skipped: str = ""                      # not counted on its own (skipped, or folded into another sheet)
    merged: bool = False                   # skipped because its pages went into another sheet of the same child
    own_pages: int | None = None           # this sheet's own pages (pages becomes the child's month count)
    sources: list = field(default_factory=list)       # sheets whose pages make up this one's count (default: itself)
    dup: dict | None = None                # for a later sheet of the same child: what the choice is between
    warnings: list = field(default_factory=list)      # shown to the person, never block (e.g. month on the sheet)


@dataclass
class Plan:
    settled: list[Settled]
    issues: list[Issue]
    matches: dict[str, mt.Match]
    pending: dict = field(default_factory=dict)       # "new@<sheet>" -> ck.Student being added this month
    resolved: dict = field(default_factory=dict)      # set by finalize: sheet key -> student id it counted for

    def student(self, cp: ck.Checkpoint, sid: str) -> ck.Student | None:
        return cp.students.get(sid) or self.pending.get(sid)

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


NEW = "new@"


def _pending_students(records, decisions) -> tuple[dict, dict]:
    """Children being added as new this month, one per sheet that says so: ({"new@<key>": Student},
    {"new@<key>": canonical id}). Two sheets that both add the same name and teacher are the same child."""
    pending, canon, seen = {}, {}, {}
    for rec in records:
        d = decisions.get(rec.key, {})
        n = d.get("new_student") if not d.get("skip") and "student" not in d else None
        if not n:
            continue
        name = (n.get("name") or rec.name).strip()
        if not name:
            continue
        sid = NEW + rec.key
        teacher = (n.get("teacher") or "").strip()
        pending[sid] = ck.Student(sid, name, teacher, str(n.get("grade", "") or ""), int(n.get("total", 0) or 0),
                                  notes=f"new, from {rec.label}")
        same = mt.normalize(name) + "|" + mt.normalize(teacher)
        canon[sid] = seen.setdefault(same, sid)
    return pending, canon


def _identity(s: Settled, canon: dict) -> str:
    if s.student_id:
        return canon.get(s.student_id, s.student_id)
    return canon.get(NEW + s.record.key, NEW + s.record.key)


def new_from(sid: str) -> str:
    """'new@oct.pdf#3' -> 'oct.pdf#3' (the sheet the new child was added from)."""
    return sid[len(NEW):] if sid.startswith(NEW) else ""


def plan(records: list[SheetRecord], cp: ck.Checkpoint, batch_month: str, decisions: dict) -> Plan:
    batch = ck.month_key(batch_month)
    settled, issues, matches = [], [], {}
    teachers = sorted({s.teacher for s in cp.students.values() if s.teacher})
    pending, canon = _pending_students(records, decisions)
    labels = {r.key: r.label for r in records}

    for rec in records:
        d = decisions.get(rec.key, {})
        if d.get("skip"):
            settled.append(Settled(rec, skipped=str(d["skip"])))
            continue
        item = Settled(rec)

        # who is this?
        m = mt.match(rec.name, rec.teacher, cp, [p for k, p in pending.items() if k != NEW + rec.key])
        matches[rec.key] = m
        if "student" in d:
            sid = d["student"]
            if sid.startswith(NEW) and (sid not in pending or sid == NEW + rec.key):
                was = labels.get(new_from(sid), new_from(sid))
                issues.append(Issue(rec.key, "who", f"{rec.label}: this was the new student from {was}, "
                                                    f"who is no longer being added; choose again", m.candidates))
            elif not sid.startswith(NEW) and sid not in cp.students:
                issues.append(Issue(rec.key, "who", f"{rec.label}: decision names unknown student {sid}",
                                    m.candidates))
            else:
                item.student_id = sid
                item.remember_name = bool(d.get("remember_name")) and sid in cp.students
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
        if item.student_id and item.student_id.startswith(NEW):
            pass                                   # the class was chosen on the sheet that added them
        elif item.student_id and (d.get("teacher") or "").strip():
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
        item.own_pages = item.pages
        if item.pages is not None and item.pages < 0:
            issues.append(Issue(rec.key, "pages", f"{rec.label}: pages cannot be negative"))

        # the month on the sheet is often wrong or a date; the batch month is used, a mismatch is only shown
        if rec.month and not sheet_month_matches(rec.month, batch):
            item.warnings.append(f"sheet says {rec.month!r}; counted for {ck.month_label(batch)}")
        settled.append(item)

    # several sheets for the same child: page order; each later sheet adds to, replaces or skips the count so far
    groups: dict[str, list[Settled]] = {}
    for item in settled:
        if not item.skipped and (item.student_id or item.new_student):
            groups.setdefault(_identity(item, canon), []).append(item)
    for ident, items in groups.items():
        if len(items) < 2:
            continue
        holder = items[0]
        count, sources = holder.pages, [holder.record]
        known_as = cp.students.get(holder.student_id) or pending.get(holder.student_id or "")
        who = known_as.name if known_as else holder.new_student["name"]
        for item in items[1:]:
            choice = decisions.get(item.record.key, {}).get("duplicate")
            before = [r.label for r in sources]
            known = count is not None and item.pages is not None
            item.dup = {"others": before, "so_far": count, "this": item.pages,
                        "sum": count + item.pages if known else None, "replace": item.pages}
            if choice == "sum":
                count = count + item.pages if known else None
                sources.append(item.record)
                item.skipped, item.merged = "added to " + holder.record.label, True
            elif choice == "replace":
                count, sources = item.pages, [item.record]
                item.skipped, item.merged = "replaces " + ", ".join(before), True
            elif choice == "skip":
                item.skipped = "same sheet as " + ", ".join(before)
            else:
                issues.append(Issue(item.record.key, "duplicate",
                                    f"{item.record.label}: {who} also has {', '.join(before)} this month "
                                    f"({count if count is not None else '?'} pages so far, this sheet "
                                    f"{item.pages if item.pages is not None else '?'}): add, replace, or skip?",
                                    (count, item.pages)))
        holder.pages, holder.sources = count, sources
    return Plan(settled, issues, matches, pending)


# ------------------------------------------------------------------ finishing
def finalize(p: Plan, cp: ck.Checkpoint, table=None) -> tuple[ck.Checkpoint, list[ck.Entry], list[str]]:
    """(checkpoint with new students added, entries, summary lines). Raises while anything is open."""
    if p.issues:
        raise ck.CheckpointError(f"{len(p.issues)} question(s) still open; first: {p.issues[0].message}")
    cp = cp.copy()
    entries, notes, created = [], [], {}
    canon = {}
    for sid, st in p.pending.items():              # same name and teacher on two "new" sheets: one child
        canon[sid] = next(o for o, ost in p.pending.items()
                          if (mt.normalize(ost.name), mt.normalize(ost.teacher)) == (mt.normalize(st.name),
                                                                                       mt.normalize(st.teacher)))
    for item in p.settled:
        if item.student_id in cp.students and item.remember_name and not item.skipped:
            old = cp.students[item.student_id]
            written = item.record.name.strip()
            known = {mt.normalize(old.name)} | {mt.normalize(n) for n in old.nickname_list}
            if written and mt.normalize(written) not in known:
                cp.students[old.id] = ck.replace(old, nicknames=", ".join(old.nickname_list + [written]))
                notes.append(f"{old.name} will also be recognised as {written!r} in {old.teacher}'s class "
                             f"({item.record.label})")
    for item in p.settled:
        if item.student_id in cp.students and item.set_teacher and not item.skipped:
            old = cp.students[item.student_id]
            cp.students[old.id] = ck.replace(old, teacher=item.set_teacher)
            change = f"changed from {old.teacher} to" if old.teacher else "set to"
            notes.append(f"teacher for {old.name} {change} {item.set_teacher} ({item.record.label})")
    for item in p.settled:
        rec = item.record
        if item.skipped:
            notes.append(f"{rec.label} ({rec.name or 'no name'}): {item.skipped}" if item.merged else
                         f"skipped {rec.label} ({rec.name or 'no name'}): {item.skipped}")
            continue
        sid = item.student_id
        if sid is None or sid.startswith(NEW):
            ident = canon.get(sid or NEW + rec.key)
            if ident not in created:
                n = p.pending[ident]
                s = ck.add_student(cp, n.name, n.teacher, n.grade, n.total,
                                   notes=f"added {p.pending[ident].notes.split('from ')[-1]}")
                created[ident] = s.id
                notes.append(f"new student {s.name} ({s.teacher}) from {s.notes[len('added '):]}")
            sid = created[ident]
        source = ", ".join(r.label for r in (item.sources or [rec]))
        entries.append(ck.Entry(sid, item.pages, source))
        p.resolved[rec.key] = sid
    return cp, entries, notes


# ------------------------------------------------------------------ suggestions
@dataclass
class Suggestion:
    """What the review screen pre-fills for a sheet that needs a look, and why it needs one."""
    decision: dict | None     # accepting this as-is settles the sheet; None if a person must fill something in
    reasons: list             # short, plain reasons the sheet was not settled automatically
    draft: dict = field(default_factory=dict)   # the same suggestion, possibly incomplete, for pre-filling a form
    needs_choice: bool = False  # complete, but only a person may accept it (never "Accept all"): extra sheets


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
            # remember real nicknames and first names (in the child's class); a spelling-like match is
            # usually a misreading, which is not worth remembering
            d["remember_name"] = best.student.id in cp.students and best.reason.startswith(("nickname", "first name only"))
            others = [c for c in m.candidates[1:] if c.score >= best.score - 0.1]
            why = "name hard to read" if m.status == mt.EXACT else best.reason
            if best.student.id.startswith(NEW):
                why = f"added as new from {p.pending[best.student.id].notes.split('from ')[-1]}; {best.reason}"
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
        if d.get("student") in cp.students and not cp.students[d["student"]].teacher and not d.get("teacher"):
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
        needs_choice = False
        if "duplicate" in kinds and "duplicate" not in d:
            so_far, this = item.dup["so_far"], item.dup["this"]
            d["duplicate"] = "skip" if so_far == this else "sum"
            needs_choice = True
            reasons.append(f"also has {', '.join(item.dup['others'])}: " +
                           ("same total, probably the same sheet twice" if d["duplicate"] == "skip"
                            else "add, replace or skip"))
        out[rec.key] = Suggestion(d if complete else None, reasons or [i.message for i in issues], d, needs_choice)
    return out
