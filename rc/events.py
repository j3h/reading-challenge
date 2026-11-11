"""The event log: every change to the data, as a validated, append-only record that points at its evidence.

Totals, teachers, grades and prizes are never edited in place. Each change is an event (a student added, a
teacher set, pages recorded for a month, a correction with a reason, ...); replaying the events in time
order gives the current state, and checkpoint.xlsx is just a readable snapshot of that replay.

    <data folder>/events/<device>.jsonl      one JSON event per line, one file per Mac

Each Mac writes only its own file (written whole to a temp file, then renamed), so iCloud never has two
versions of one file to choose between. Reading merges every file. Duplicates count once: the same id seen
twice (a file synced twice), and the same EVIDENCE counted twice: an event that adds something (pages,
prizes, a student, a note) and points at a document (scan page, photo, form row, workbook row) is a
duplicate of an earlier one with the same kind, data, reason and sources (the import run twice, a month
finished on two Macs). Who/when/which Mac are not part of that comparison. Two things are never merged
by content: manual events (two deliberate "+1 page" corrections are two corrections), and setting a value
(teacher, grade, name, total: Barry -> Biello -> Barry is real). So deduplication never loses information,
and `compact()` can rewrite a file without its duplicates safely. Nothing is deleted: a mistaken event is cancelled by an `event_voided`
event, and a fix is a new event with a reason.

Event kinds (data fields):
    student_added      student, name, teacher, grade
    name_set           student, name
    teacher_set        student, teacher
    grade_set          student, grade
    also_called_added  student, name
    note_added         student, text                 a remark kept on the student (e.g. "red in PRIMARY")
    student_left       student
    total_set          student, total               an opening balance or a reconciliation; needs a reason
    pages_recorded     student, month, pages         a month's reading (from sheets, a form, the workbook)
    pages_adjusted     student, month, delta         a correction to a month's pages; needs a reason
    prizes_given       student, month, levels        level thresholds handed out
    event_voided       event                         cancels another event; needs a reason
    month_finished     month                         a month was finished (which Mac, when)

`sources` is a list of dicts saying where an event came from, e.g. {"scan": "oct.pdf", "page": 12},
{"photo": "emailed 1.jpg"}, {"form": "Responses", "submitted": "2026-11-02 16:11"},
{"workbook": "master.xlsx", "tab": "September 2026", "row": 15}, {"manual": true}.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rc import checkpoint as ck
from rc import workspace as wsp

EVENTS_DIR = "events"
KINDS = {
    "student_added": ("student", "name", "teacher", "grade"),
    "name_set": ("student", "name"),
    "teacher_set": ("student", "teacher"),
    "grade_set": ("student", "grade"),
    "also_called_added": ("student", "name"),
    "note_added": ("student", "text"),
    "student_left": ("student",),
    "total_set": ("student", "total"),
    "pages_recorded": ("student", "month", "pages"),
    "pages_adjusted": ("student", "month", "delta"),
    "prizes_given": ("student", "month", "levels"),
    "event_voided": ("event",),
    "month_finished": ("month",),
}
NEEDS_REASON = {"total_set", "pages_adjusted", "event_voided"}
SETTERS = {"name_set": "name", "teacher_set": "teacher", "grade_set": "grade"}
ADDITIVE = {"student_added", "also_called_added", "note_added", "pages_recorded", "pages_adjusted", "prizes_given",
            "event_voided", "month_finished"}                  # deduplicated by content as well as by id
CLASH_WINDOW = dt.timedelta(days=1)


class EventError(ck.CheckpointError):
    pass


def dedupable(e: "Event") -> bool:
    """Merged with an identical earlier event? Only additive events that point at a document."""
    return e.kind in ADDITIVE and any(not src.get("manual") for src in e.sources)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="microseconds")   # orders events made in one second


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict
    who: str = ""
    reason: str = ""
    sources: tuple = ()
    time: str = field(default_factory=now)
    device: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    batch: str = ""            # events written together (e.g. one month's finish), so they can be cancelled together

    def to_json(self) -> str:
        d = asdict(self)
        d["sources"] = list(self.sources)
        return json.dumps(d, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def from_json(line: str) -> "Event":
        d = json.loads(line)
        d["sources"] = tuple(d.get("sources") or ())
        return Event(**d)

    @property
    def student(self) -> str:
        return self.data.get("student", "")

    @property
    def fingerprint(self) -> str:
        """What the event says, without who/when/where: equal fingerprints are the same change recorded twice."""
        return json.dumps([self.kind, self.data, self.reason.strip(), sorted(json.dumps(x, sort_keys=True)
                                                                             for x in self.sources)],
                          sort_keys=True, ensure_ascii=False)

    def describe(self) -> str:
        """One line a person can read."""
        d = self.data
        text = {
            "student_added": lambda: f"added {d['name']} ({d['teacher'] or 'no teacher'}, grade {d['grade'] or '?'})",
            "name_set": lambda: f"name set to {d['name']}",
            "teacher_set": lambda: f"teacher set to {d['teacher']}",
            "grade_set": lambda: f"grade set to {d['grade']}",
            "also_called_added": lambda: f"also called {d['name']!r}",
            "note_added": lambda: f"note: {d['text']}",
            "student_left": lambda: "marked as left",
            "total_set": lambda: f"total set to {d['total']:,}",
            "pages_recorded": lambda: f"{ck.month_label(d['month'])}: {d['pages']:,} pages",
            "pages_adjusted": lambda: f"{ck.month_label(d['month'])}: pages {d['delta']:+,}",
            "prizes_given": lambda: f"prizes handed out: {', '.join(f'{t:,}' for t in d['levels'])}",
            "event_voided": lambda: f"cancelled event {d['event'][:8]}",
            "month_finished": lambda: f"{ck.month_label(d['month'])} finished",
        }[self.kind]()
        return text + (f" - {self.reason}" if self.reason else "")


def validate_shape(e: Event) -> None:
    if e.kind not in KINDS:
        raise EventError(f"unknown event kind {e.kind!r}")
    missing = [k for k in KINDS[e.kind] if k not in e.data]
    if missing:
        raise EventError(f"{e.kind}: missing {', '.join(missing)}")
    if e.kind in NEEDS_REASON and not e.reason.strip():
        raise EventError(f"{e.kind} needs a reason")
    if not e.who.strip():
        raise EventError("every event needs a name for who made it")
    if "month" in e.data:
        e.data["month"] = ck.month_key(e.data["month"])
    for k in ("pages", "total"):
        if k in e.data and (not isinstance(e.data[k], int) or e.data[k] < 0):
            raise EventError(f"{e.kind}: {k} must be a whole number, 0 or more")
    if e.kind == "pages_adjusted" and (not isinstance(e.data["delta"], int) or e.data["delta"] == 0):
        raise EventError("pages_adjusted: delta must be a non-zero whole number")


# ---------------------------------------------------------------------------- replay
@dataclass
class State:
    students: dict = field(default_factory=dict)              # id -> ck.Student
    history: list = field(default_factory=list)               # ck.HistoryRow, in time order
    month_pages: dict = field(default_factory=dict)           # (id, month) -> pages (after adjustments)
    prizes_given: dict = field(default_factory=dict)          # id -> set of thresholds
    finished: dict = field(default_factory=dict)              # month -> month_finished event
    voided: set = field(default_factory=set)
    by_student: dict = field(default_factory=dict)            # id -> [Event] (live and voided, time order)
    warnings: list = field(default_factory=list)
    events: list = field(default_factory=list)                # every distinct event, time order
    duplicates: list = field(default_factory=list)            # (later copy, the earlier one it repeats)

    def to_checkpoint(self) -> ck.Checkpoint:
        months = list(self.finished) + [h.month for h in self.history]
        return ck.Checkpoint(max(months) if months else None, dict(self.students), list(self.history))

    def owed(self, table) -> dict:
        """id -> [(threshold, label, prize)] crossed by the total but never handed out."""
        out = {}
        for sid, s in self.students.items():
            given = self.prizes_given.get(sid, set())
            missing = [lv for lv in table if lv[0] <= s.total and lv[0] not in given]
            if missing:
                out[sid] = missing
        return out


def _order(e: Event):
    return (e.time, e.id)


def replay(events) -> State:
    """Apply events in time order. Invalid events (e.g. pages for a student never added) are skipped and
    reported in `warnings`, so one bad line never hides the rest."""
    st = State()
    events = sorted({e.id: e for e in events}.values(), key=_order)        # a duplicated id counts once
    first: dict[str, Event] = {}
    distinct = []
    for e in events:                                   # so does the same evidence counted twice
        if dedupable(e) and e.fingerprint in first:
            st.duplicates.append((e, first[e.fingerprint]))
        else:
            first[e.fingerprint] = e
            distinct.append(e)
    st.events = events = distinct
    st.voided = {e.data["event"] for e in events if e.kind == "event_voided"}
    last_set: dict = {}
    for e in events:
        if e.student:
            st.by_student.setdefault(e.student, []).append(e)
        if e.id in st.voided or e.kind == "event_voided":
            continue
        try:
            _apply(st, e, last_set)
        except EventError as err:
            st.warnings.append(f"{e.time} {e.kind} by {e.who}: {err}")
    return st


def _apply(st: State, e: Event, last_set: dict) -> None:
    d, sid = e.data, e.student
    if e.kind == "month_finished":
        if d["month"] in st.finished and st.finished[d["month"]].device != e.device:
            st.warnings.append(f"{ck.month_label(d['month'])} was finished on two Macs "
                               f"({st.finished[d['month']].device}, {e.device}): check for double counting")
        st.finished.setdefault(d["month"], e)
        return
    if e.kind == "student_added":
        if sid in st.students:
            raise EventError(f"{sid} already exists")
        st.students[sid] = ck.Student(sid, d["name"], d["teacher"], str(d.get("grade", "")), 0)
        return
    if sid not in st.students:
        raise EventError(f"no student {sid}")
    s = st.students[sid]
    if e.kind in SETTERS:
        attr = SETTERS[e.kind]
        prev = last_set.get((sid, attr))
        if prev and prev.device != e.device and prev.data[attr] != d[attr] and \
                _time(e) - _time(prev) < CLASH_WINDOW:
            st.warnings.append(f"{s.name}: {attr} set to {prev.data[attr]!r} ({prev.who}, {prev.device}) and "
                               f"{d[attr]!r} ({e.who}, {e.device}) within a day; using the later one")
        last_set[(sid, attr)] = e
        st.students[sid] = ck.replace(s, **{attr: d[attr]})
    elif e.kind == "also_called_added":
        if d["name"].strip().lower() not in {n.lower() for n in s.nickname_list}:
            st.students[sid] = ck.replace(s, nicknames=", ".join(s.nickname_list + [d["name"].strip()]))
    elif e.kind == "note_added":
        st.students[sid] = ck.replace(s, notes="; ".join(x for x in (s.notes, d["text"]) if x))
    elif e.kind == "student_left":
        st.students[sid] = ck.replace(s, status=ck.LEFT)
    elif e.kind == "total_set":
        st.students[sid] = ck.replace(s, total=d["total"])
    elif e.kind in ("pages_recorded", "pages_adjusted"):
        change = d["pages"] if e.kind == "pages_recorded" else d["delta"]
        if s.total + change < 0:
            raise EventError(f"{s.name}'s total would go below 0")
        key = (sid, d["month"])
        st.month_pages[key] = st.month_pages.get(key, 0) + change
        st.students[sid] = ck.replace(s, total=s.total + change)
        st.history.append(ck.HistoryRow(d["month"], sid, change, s.total + change, _source_text(e)))
    elif e.kind == "prizes_given":
        st.prizes_given.setdefault(sid, set()).update(int(t) for t in d["levels"])


def _time(e: Event) -> dt.datetime:
    return dt.datetime.fromisoformat(e.time)


def sheet_source(file: str, page: int, submitted: str = "") -> dict:
    """Where a reviewed sheet came from: a page of a scanned PDF, a photo, or a row of form responses."""
    if file.lower().endswith(".csv"):
        return {"form": file, "row": page, "submitted": submitted}
    return {"scan": file, "page": page} if file.lower().endswith(".pdf") else {"photo": file}


def _source_text(e: Event) -> str:
    parts = []
    for s in e.sources:
        if "scan" in s:
            parts.append(f"{s['scan']} p. {s.get('page', '?')}")
        elif "photo" in s:
            parts.append(f"photo {s['photo']}")
        elif "form" in s:
            parts.append(f"form {s['form']} row {s.get('row', '?')}" + (f" ({s['submitted']})" if s.get("submitted") else ""))
        elif "workbook" in s:
            parts.append(f"{s.get('tab', s['workbook'])}" + (f" row {s['row']}" if s.get("row") else ""))
        elif s.get("manual"):
            parts.append("manual")
    return ", ".join(parts) or e.kind


# ---------------------------------------------------------------------------- the files
def device_name() -> str:
    import socket
    from rc import settings
    name = settings.load().get("device") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "this-mac"


class EventLog:
    """The events in a data folder. `append` validates against a fresh replay before writing anything."""

    def __init__(self, root, device: str | None = None):
        self.root = Path(root)
        self.dir = self.root / EVENTS_DIR
        self.device = device or device_name()

    @property
    def own_file(self) -> Path:
        return self.dir / f"{self.device}.jsonl"

    def files(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("*.jsonl") if not p.name.startswith(".")) if self.dir.is_dir() else []

    def problems(self) -> list[str]:
        """Things to warn about: files not downloaded yet (iCloud), copies of a Mac's file (any sync service)."""
        out = []
        if self.dir.is_dir():
            if wsp.cloud_placeholders(self.dir):
                out.append("some event files are still downloading from iCloud; wait, then try again")
            for p in self.files():
                # each Mac writes only <its name>.jsonl, so a file holding another name's events is a copy:
                # a sync conflict copy ("x 2", "x (1)", "x (Jamie's conflicted copy)") or a hand-made one.
                # Its events are still read (and duplicates count once), but someone should tidy it up.
                try:
                    devices = {Event.from_json(ln).device for ln in p.read_text(encoding="utf-8").splitlines()
                               if ln.strip()}
                except (json.JSONDecodeError, TypeError, OSError):
                    continue                            # read() reports damaged files
                if devices - {p.stem}:
                    out.append(f"{p.name} holds events from {', '.join(sorted(devices - {p.stem}))}: it looks like "
                               f"a copy made by the sync service; its events are read too, but tell whoever looks "
                               f"after the data folder")
        return out

    def read(self) -> list[Event]:
        events = []
        for p in self.files():
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    try:
                        events.append(Event.from_json(line))
                    except (json.JSONDecodeError, TypeError) as e:
                        raise EventError(f"{p.name} line {n} is damaged ({e}); nothing was changed") from None
        return events

    def state(self) -> State:
        return replay(self.read())

    def append(self, new: list[Event]) -> State:
        """Validate `new` against everything already recorded, then write them to this Mac's file."""
        blocking = [p for p in self.problems() if "downloading" in p]
        if blocking:
            raise EventError(blocking[0])
        new = [Event(e.kind, dict(e.data), e.who, e.reason, tuple(e.sources), e.time, self.device, e.id, e.batch)
               for e in new]
        for e in new:
            validate_shape(e)
        existing = self.read()
        known = {e.id for e in existing} | {e.fingerprint for e in existing if dedupable(e)}
        after = replay(existing + new)
        bad = [w for w in after.warnings if w not in replay(existing).warnings]
        if bad:
            raise EventError("not recorded: " + "; ".join(bad))
        lines = []
        if self.own_file.exists():
            lines = self.own_file.read_text(encoding="utf-8").splitlines()
        lines += [e.to_json() for e in new if e.id not in known and not (dedupable(e) and e.fingerprint in known)]
        wsp.write_atomic(self.own_file, "\n".join(lines) + "\n")
        return after

    def compact(self) -> int:
        """Rewrite this Mac's file without events that repeat one already recorded (anywhere). Returns how
        many were dropped. Safe: a dropped event changes nothing the replay computes."""
        st = self.state()
        drop = {dup.id for dup, _ in st.duplicates if dup.device == self.device}
        if not drop or not self.own_file.exists():
            return 0
        lines = [ln for ln in self.own_file.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and Event.from_json(ln).id not in drop]
        wsp.write_atomic(self.own_file, "\n".join(lines) + "\n")
        return len(drop)

    def has_students(self) -> bool:
        return any(e.kind == "student_added" for e in self.read())

    def live_finish(self, month: str, state: "State | None" = None) -> "Event | None":
        """The month_finished event that currently counts for `month` (None if the month is open)."""
        st = state or self.state()
        return st.finished.get(ck.month_key(month))

    def timeline(self, student_id: str) -> list[tuple[Event, bool]]:
        """(event, still counts) for one student, oldest first, including cancelled ones."""
        st = self.state()
        return [(e, e.id not in st.voided) for e in st.by_student.get(student_id, [])]
