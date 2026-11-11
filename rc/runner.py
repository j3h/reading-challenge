"""Run a month: read the month's records and decisions, apply them, write the checkpoint, PDF and summary.

The month's folder (see workspace.py) holds everything. `work/records.json` is what extraction read from
the scans; `work/decisions.json` is what a person settled in review. Both can be re-run safely: nothing is
written to the month's final files until every question is answered, and the checkpoint is written last,
so a month counts as finished only when all three outputs exist.
"""
from __future__ import annotations

from collections import Counter

import make_certificates as mc
from rc import audit
from rc import checkpoint as ck
from rc import events as ev
from rc import render
from rc import review as rv
from rc import workspace as wsp

RECORDS = "records.json"
DECISIONS = "decisions.json"


class Month:
    """Everything needed to look at or finish one month."""

    def __init__(self, workspace: wsp.Workspace, month: str, table=None):
        self.ws = workspace
        self.folder = workspace.month_folder(month)
        self.table = table or mc.load_levels()
        self.records_path = self.folder.work / RECORDS
        self.decisions_path = self.folder.work / DECISIONS
        self.log = ev.EventLog(workspace.root)

    # ---- inputs
    def uses_events(self) -> bool:
        """A data folder with an event log keeps everything there; older folders use checkpoint files."""
        return self.log.has_students()

    def previous(self) -> tuple[ck.Checkpoint, str]:
        if not self.uses_events():
            path, why = self.ws.previous_checkpoint(self.folder.month, self.folder.season)
            return (ck.load(path) if path else ck.Checkpoint()), why
        state = self.log.state()
        month = self.folder.month
        if month in state.finished:
            raise ck.CheckpointError(f"{ck.month_label(month)} is already finished in the event log; reopen it to "
                                     f"change it")
        later = sorted(m for m in state.finished if m > month)
        if later:
            raise ck.CheckpointError(f"{ck.month_label(month)} is older than the already finished "
                                     f"{ck.month_label(later[0])}; reopen that month first")
        cp = state.to_checkpoint()
        cp.month = max((h.month for h in cp.history if h.month < month), default=None)
        return cp, f"the event log ({len(state.events)} events, as of now)"

    def records(self) -> list[rv.SheetRecord]:
        """Every sheet for the month: scanned pages and photos as read (records.json), then this month's rows of
        any form-response CSVs in the scans folder (read fresh each time: typed, so nothing to pay for)."""
        from rc import forms
        forms_files = self.folder.form_files() if self.folder.scans.is_dir() else []
        if not self.records_path.exists() and not forms_files:
            raise ck.CheckpointError(f"no sheets have been read yet: {self.records_path} is missing "
                                     f"(scans go in {self.folder.scans})")
        out = rv.load_records(self.records_path) if self.records_path.exists() else []
        for f in forms_files:
            out += forms.records(f, self.folder.month)[0]
        return out

    def decisions(self) -> dict:
        return rv.load_decisions(self.decisions_path)

    def plan(self) -> rv.Plan:
        if self.folder.is_finished():
            raise ck.CheckpointError(f"{ck.month_label(self.folder.month)} is already finished "
                                     f"({self.folder.checkpoint})")
        cp, _ = self.previous()
        return rv.plan(self.records(), cp, self.folder.month, self.decisions())

    # ---- review
    def decide(self, key: str, **decision) -> None:
        """Record (merge into) the decision for one sheet and save."""
        records = {r.key for r in self.records()}
        if key not in records:
            raise ck.CheckpointError(f"no sheet {key!r}; sheets are {', '.join(sorted(records)[:5])} ...")
        decisions = self.decisions()
        decisions.setdefault(key, {}).update(decision)
        rv.save_decisions(decisions, self.decisions_path, "command line")

    def set_decision(self, key: str, decision: dict, source: str = "review screen") -> None:
        """Replace the whole decision for one sheet (an empty decision removes it) and save."""
        if key not in {r.key for r in self.records()}:
            raise ck.CheckpointError(f"no sheet {key!r}")
        decisions = self.decisions()
        if decision:
            decisions[key] = decision
        else:
            decisions.pop(key, None)
        rv.save_decisions(decisions, self.decisions_path, source)

    # ---- finishing
    def finish(self, who: str = "") -> str:
        """Write certificates.pdf and summary.txt, record the month in the event log (when the folder has one),
        then checkpoint.xlsx last. Returns the summary text."""
        events_mode = self.uses_events()
        if events_mode and not who.strip():
            raise ck.CheckpointError("say who is finishing the month (your name in Settings, or --who)")
        p = self.plan()
        if not p.ready:
            raise ck.CheckpointError(f"{len(p.issues)} question(s) still open; run 'check' to list them")
        before, why = self.previous()
        cp_with_new, entries, notes = rv.finalize(p, before, self.table)
        if not entries:
            raise ck.CheckpointError("every sheet was skipped: nothing to make")
        new_cp, results = ck.apply_month(cp_with_new, self.folder.month, entries, self.table)
        label = ck.month_label(self.folder.month)

        rows = audit.sheet_rows(p, self.decisions(), results)
        checked = audit.checks(self.records(), p, before, cp_with_new, new_cp, results, self.table)
        spots = audit.spot_checks(rows, self.folder.month)
        owed = {}                                   # prizes crossed earlier but never handed out: bag them now
        if events_mode:
            state = self.log.state()
            owed = {sid: lv for sid, lv in state.owed(self.table).items() if state.students[sid].status == ck.ACTIVE}
        owed_rows = [(new_cp.students[sid], lv) for sid, lv in owed.items()]
        if owed:
            notes = notes + [f"owed from earlier, bagged now: {new_cp.students[sid].name}: "
                             + ", ".join(f"{p} ({t:,})" for t, _, p in lv) for sid, lv in owed.items()]
        events = month_events(p, before, cp_with_new, results, self.folder.month, who, owed) if events_mode else []
        if events_mode:                       # the log, replayed, must say exactly what this month computed
            replayed = ev.replay(self.log.read() + events)
            bad = [r.student.name for r in results if replayed.students[r.student.id].total != r.student.total]
            checked.append((not bad and not replayed.warnings,
                            "the event log, replayed, gives the same totals" +
                            (f" (differs for: {', '.join(bad)})" if bad else "") +
                            (f" (warnings: {'; '.join(replayed.warnings)})" if replayed.warnings else "")))

        self.folder.create()
        warnings: list[str] = []
        wsp.save_atomic(lambda tmp: warnings.extend(render.render_print_pdf(results, label, tmp, self.table, owed_rows)),
                        self.folder.certificates)
        text = summary(label, why, before, new_cp, results, notes, warnings, self.table, audit.report(checked, spots),
                       owed_rows)
        wsp.write_atomic(self.folder.summary, text)
        if events_mode:
            if not all(ok for ok, _ in checked):
                raise ck.CheckpointError("the checks failed (see summary.txt); nothing was recorded in the event log")
            new_cp = self.log.append(events).to_checkpoint()
        wsp.save_atomic(lambda tmp: ck.save(new_cp, tmp, results, self.table,
                                            sheets=(audit.SHEET_COLS, [r.cells() for r in rows])),
                        self.folder.checkpoint)
        return text


def reopen_month(m: "Month", who: str, reason: str = "") -> int:
    """Cancel the events a month's finish recorded (event_voided, one per event, same reason). Returns how many.
    Refused while a later month is finished: it started from this month's totals."""
    state = m.log.state()
    month = m.folder.month
    fin = state.finished.get(month)
    if fin is None:
        return 0
    later = sorted(k for k in state.finished if k > month)
    if later:
        raise ck.CheckpointError(f"{ck.month_label(later[0])} was finished after this month and started from its "
                                 f"totals; reopen that one first")
    if not who.strip():
        raise ck.CheckpointError("say who is reopening the month (your name in Settings, or --who)")
    why = f"reopened {ck.month_label(month)}" + (f": {reason.strip()}" if reason.strip() else "")
    targets = [e for e in state.events if e.batch == fin.batch and e.id not in state.voided]
    m.log.append([ev.Event("event_voided", {"event": e.id}, who, why, ({"manual": True},)) for e in targets])
    return len(targets)


def month_events(p: rv.Plan, before: ck.Checkpoint, with_new: ck.Checkpoint, results, month: str,
                 who: str, owed: dict | None = None) -> list[ev.Event]:
    """What finishing a month adds to the log, each change pointing at the sheet(s) it came from. All share one
    batch id (the month_finished event's), so reopening the month cancels exactly these."""
    import datetime as dt
    start = dt.datetime.now().astimezone()
    # events apply in time order, so each gets its own moment, in the order written (a new student before
    # their pages)
    tick = lambda: (start + dt.timedelta(microseconds=len(out))).isoformat(timespec="microseconds")
    out: list[ev.Event] = []
    finish = ev.Event("month_finished", {"month": month, "sheets": len(p.settled)}, who, "", (), tick())
    batch = finish.id
    out.append(ev.Event(finish.kind, finish.data, who, "", (), finish.time, "", finish.id, batch))
    mk = lambda kind, data, src, reason="": out.append(ev.Event(kind, data, who, reason, tuple(src),
                                                                tick(), "", batch=batch))
    src_of = lambda rec: ev.sheet_source(rec.file, rec.page, rec.submitted)
    added = set()
    for item in p.settled:
        rec = item.record
        sid = p.resolved.get(rec.key)
        if not sid or item.skipped:
            continue
        if sid not in before.students and sid not in added:
            s = with_new.students[sid]
            mk("student_added", {"student": sid, "name": s.name, "teacher": s.teacher, "grade": s.grade},
               [src_of(r) for r in (item.sources or [rec])], "new this month")
            added.add(sid)
        if item.set_teacher and sid in before.students:
            mk("teacher_set", {"student": sid, "teacher": item.set_teacher}, [src_of(rec)],
               f"sheet says {rec.teacher!r}")
        if item.remember_name and sid in before.students:
            known = {mt_norm(before.students[sid].name)} | {mt_norm(n) for n in before.students[sid].nickname_list}
            if rec.name.strip() and mt_norm(rec.name) not in known:
                mk("also_called_added", {"student": sid, "name": rec.name.strip()}, [src_of(rec)])
    by_student = {r.student.id: r for r in results}
    for item in p.settled:
        sid = p.resolved.get(item.record.key)
        if sid and not item.skipped and sid in by_student:
            r = by_student[sid]
            mk("pages_recorded", {"student": sid, "month": month, "pages": r.pages},
               [src_of(x) for x in (item.sources or [item.record])])
            if r.prizes:
                mk("prizes_given", {"student": sid, "month": month, "levels": [t for t, _, _ in r.prizes]},
                   [src_of(x) for x in (item.sources or [item.record])])
    for sid, levels in (owed or {}).items():
        mk("prizes_given", {"student": sid, "month": month, "levels": [t for t, _, _ in levels]}, [],
           f"owed from earlier; bagged with {ck.month_label(month)}")
    return out


def mt_norm(name: str) -> str:
    from rc import matching as mt
    return mt.normalize(name)


def describe_issues(p: rv.Plan) -> list[str]:
    lines = []
    for i in p.issues:
        lines.append(f"[{i.kind}] {i.message}")
        if i.kind == "who":
            for c in i.candidates[:3]:
                lines.append(f"      {c.student.id}  {c.student.name} / {c.student.teacher} - {c.reason}")
        elif i.kind == "teacher":
            lines.append("      teachers: " + ", ".join(i.candidates))
    return lines


def summary(label, source, before, after, results, notes, warnings, table, checks=(), owed_rows=()) -> str:
    new_ids = set(after.students) - set(before.students)
    prizes = Counter()
    for r in results:
        for p in r.prizes:
            prizes[p[2]] += 1
    for _, levels in owed_rows:
        for p in levels:
            prizes[p[2]] += 1
    absent = ck.not_seen(after, results)
    out = [f"Reading Challenge - {label}", "=" * 40, "",
           f"Started from: {source}",
           f"Certificates: {len(results)}   Classrooms: {len(ck.classrooms(results))}   "
           f"New students: {len(new_ids)}   Pages read: {sum(r.pages for r in results):,}", ""]
    if checks:
        out += list(checks) + [""]
    n_owed = sum(len(lv) for _, lv in owed_rows)
    out.append("Prizes to hand out" + ("" if prizes else ": none")
               + (f" (including {n_owed} owed from earlier months)" if n_owed else ""))
    order = {}
    for threshold, _, prize in table:
        order.setdefault(prize, threshold)
    for prize, n in sorted(prizes.items(), key=lambda kv: (order.get(kv[0], 10**9), kv[0])):
        out.append(f"  {n:>3} x {prize}")
    out += ["", "By classroom"]
    for teacher, rs in ck.classrooms(results):
        with_prize = sum(1 for r in rs if r.prizes)
        out.append(f"  {teacher}: {len(rs)} {'student' if len(rs) == 1 else 'students'}, {with_prize} with prizes")
    if notes:
        out += ["", "What changed in review"] + [f"  - {n}" for n in notes]
    if warnings:
        out += ["", "Layout notes (check these pages when printed)"] + [f"  - {w}" for w in sorted(set(warnings))]
    if absent:
        out += ["", f"No sheet this month ({len(absent)}): carried forward unchanged"]
        out += [f"  - {s.name} ({s.teacher or 'no teacher'})" for s in absent]
    return "\n".join(out) + "\n"
