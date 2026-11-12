"""Run a month: read the month's records and decisions, apply them, write the checkpoint, PDF and summary.

The month's folder (see workspace.py) holds everything. `work/records.json` is what extraction read from
the scans; `work/decisions.json` is what a person settled in review. Both can be re-run safely: nothing is
written to the month's final files until every question is answered, and the checkpoint is written last,
so a month counts as finished only when all three outputs exist.
"""
from __future__ import annotations

import re
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
    def teacher_grades(self) -> dict:
        """teacher -> grade from the log (empty for folders without one)."""
        return self.log.state().teacher_grades if self.uses_events() else {}

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
        if self.uses_events() and not who.strip():
            raise ck.CheckpointError("say who is finishing the month (your name in Settings, or --who)")
        p = self.plan()
        if not p.ready:
            raise ck.CheckpointError(f"{len(p.issues)} question(s) still open; run 'check' to list them")
        return self._write(p, self.folder, self.folder.month, who, self.records(), ck.month_label(self.folder.month))

    def _write(self, p: rv.Plan, out, month: str, who: str, records, title: str, finish_data: dict | None = None) -> str:
        """Apply a settled plan for `month`: the print file and summary in `out`, the event log, the checkpoint."""
        events_mode = self.uses_events()
        before, why = self.previous()
        cp_with_new, entries, notes = rv.finalize(p, before, self.table)
        if not entries:
            raise ck.CheckpointError("every sheet was skipped: nothing to make")
        new_cp, results = ck.apply_month(cp_with_new, month, entries, self.table)
        label = ck.month_label(month)

        rows = audit.sheet_rows(p, self.decisions(), results)
        checked = audit.checks(records, p, before, cp_with_new, new_cp, results, self.table)
        spots = audit.spot_checks(rows, month)
        owed = {}                                   # prizes crossed earlier but never handed out: bag them now
        if events_mode:
            state = self.log.state()
            owed = {sid: lv for sid, lv in state.owed(self.table).items() if state.students[sid].status == ck.ACTIVE}
        owed_rows = [(new_cp.students[sid], lv) for sid, lv in owed.items()]
        if owed:
            notes = notes + [f"owed from earlier, bagged now: {new_cp.students[sid].name}: "
                             + ", ".join(f"{p} ({t:,})" for t, _, p in lv) for sid, lv in owed.items()]
        events = month_events(p, before, cp_with_new, results, month, who, owed,
                              state.teacher_grades if events_mode else {}, finish_data) if events_mode else []
        if events_mode:                       # the log, replayed, must say exactly what this month computed
            replayed = ev.replay(self.log.read() + events)
            bad = [r.student.name for r in results if replayed.students[r.student.id].total != r.student.total]
            checked.append((not bad and not replayed.warnings,
                            "the event log, replayed, gives the same totals" +
                            (f" (differs for: {', '.join(bad)})" if bad else "") +
                            (f" (warnings: {'; '.join(replayed.warnings)})" if replayed.warnings else "")))

        reprints = reprint_map(state, results, month, self.table) if events_mode else {}
        for sid, (pages, _) in reprints.items():
            notes = notes + [f"{new_cp.students[sid].name}: certificate printed again with all of {label} "
                             f"({pages:,} pages); it replaces the earlier one"]
        out.create()
        warnings: list[str] = []
        # one file to print: the class lists for bagging, then the certificates two to a sheet (cut and stack)
        wsp.save_atomic(lambda tmp: warnings.extend(render.render_print_file(results, label, tmp, self.table, owed_rows,
                                                                             reprints)),
                        out.certificates)
        text = summary(title, why, before, new_cp, results, notes, warnings, self.table, audit.report(checked, spots),
                       owed_rows)
        wsp.write_atomic(out.summary, text)
        if events_mode:
            if not all(ok for ok, _ in checked):
                raise ck.CheckpointError("the checks failed (see summary.txt); nothing was recorded in the event log")
            new_cp = self.log.append(events).to_checkpoint()
        wsp.save_atomic(lambda tmp: ck.save(new_cp, tmp, results, self.table,
                                            sheets=(audit.SHEET_COLS, [r.cells() for r in rows])),
                        out.checkpoint)
        return text


POOL = "pool.json"


def previous_calendar_month(today=None) -> str:
    import datetime as dt
    d = today or dt.date.today()
    return f"{d.year - (d.month == 1)}-{12 if d.month == 1 else d.month - 1:02d}"


class Pool(Month):
    """The inbox: every sheet waits here until a round prints it.

    A sheet is pending (a question is open), ready (settled), held (settled or not, kept for a later round) or
    filed (a round printed it). A round takes the ready sheets, gives them a month (by default last calendar
    month: the first one a whole-month sheet can cover) and prints them; anything still open, and every other
    sheet of a child with an open sheet, waits for the next round. Late sheets for a month already printed simply
    go into a later round."""

    def __init__(self, workspace: wsp.Workspace, table=None):
        self.ws = workspace
        self.folder = workspace.inbox()
        self.table = table or mc.load_levels()
        self.records_path = self.folder.work / RECORDS
        self.decisions_path = self.folder.work / DECISIONS
        self.settings_path = self.folder.work / POOL
        self.log = ev.EventLog(workspace.root)

    # ---- settings kept with the inbox: where form rows start, and the month picked for the next round
    def settings(self) -> dict:
        import json
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        except ValueError:
            return {}

    def save_settings(self, **values) -> None:
        import json
        current = self.settings()
        current.update(values)
        current = {k: v for k, v in current.items() if v not in (None, "")}
        wsp.write_atomic(self.settings_path, json.dumps(current, indent=2))

    def latest_round_month(self, state=None) -> str:
        st = state or self.log.state()
        return max((e.data["month"] for e in st.rounds), default="")

    def default_round_month(self, state=None) -> str:
        return max(previous_calendar_month(), self.latest_round_month(state))

    @property
    def round_month(self) -> str:
        return self.settings().get("round_month") or self.default_round_month()

    def set_round_month(self, month: str) -> None:
        key = ck.month_key(month)
        latest = self.latest_round_month()
        if latest and key < latest:
            raise ck.CheckpointError(f"{ck.month_label(latest)} has already been printed; a round can be for that month "
                                     f"or a later one (late sheets for earlier months go into this round)")
        self.save_settings(round_month=key if key != self.default_round_month() else "")

    # ---- inputs
    def uses_events(self) -> bool:
        return True

    def previous(self) -> tuple[ck.Checkpoint, str]:
        if not self.log.has_students():
            raise ck.CheckpointError("this data folder has no event log yet (import the master workbook first)")
        state = self.log.state()
        cp = state.to_checkpoint()
        month = self.round_month
        cp.month = max((h.month for h in cp.history if h.month < month), default=None)
        return cp, f"the event log ({len(state.events)} events, as of now)"

    def all_records(self) -> list[rv.SheetRecord]:
        """Every sheet the inbox holds, printed or not: scans as read, then form rows (fresh from the download)."""
        from rc import forms
        out = rv.load_records(self.records_path) if self.records_path.exists() else []
        since = self.settings().get("forms_from", "")
        seen = {r.key for r in out}
        for f in (self.folder.form_files() if self.folder.scans.is_dir() else []):
            out += [r for r in forms.pool_records(f, since)[0] if r.key not in seen]
        return out

    def unfiled(self) -> list[rv.SheetRecord]:
        filed = self.log.state().filed
        return [r for r in self.all_records() if r.key not in filed]

    def held(self) -> list[rv.SheetRecord]:
        d = self.decisions()
        return [r for r in self.unfiled() if d.get(r.key, {}).get("hold")]

    def records(self) -> list[rv.SheetRecord]:
        """What review works on: sheets not printed yet and not held."""
        d = self.decisions()
        return [r for r in self.unfiled() if not d.get(r.key, {}).get("hold")]

    def plan(self) -> rv.Plan:
        cp, _ = self.previous()
        return self._plan(self.records(), cp, self.decisions())

    def _plan(self, records, cp, decisions) -> rv.Plan:
        p = rv.plan(records, cp, self.round_month, decisions)
        self._repeats(p, cp, decisions)
        return p

    def _repeats(self, p: rv.Plan, cp: ck.Checkpoint, decisions: dict) -> None:
        """A sheet for a child who already has pages for this round's month, or for the month the sheet names:
        maybe the same reading again (skip it), a late sheet better held for a later round (hold it), or really more
        reading for that month (accept it). A person chooses: it is a question until then."""
        st = self.log.state()
        month = self.round_month
        for item in p.settled:
            sid, rec = item.student_id, item.record
            if item.skipped or not sid or sid not in cp.students:
                continue
            why = ""
            got = st.month_pages.get((sid, month))
            if got:
                why = (f"already has {got:,} pages for {ck.month_label(month)} (earlier round): accept to print their "
                       f"{ck.month_label(month)} certificate again with both, hold for a later round, or skip if it's the "
                       f"same reading")
            elif rec.month:
                for (s2, m2), n in st.month_pages.items():
                    if s2 == sid and m2 != month and n and rv.sheet_month_matches(rec.month, m2):
                        why = (f"sheet says {rec.month!r}; already has {n:,} pages for {ck.month_label(m2)}: skip if "
                               f"it's the same reading, or accept to count it for {ck.month_label(month)}")
                        break
            if not why:
                continue
            if decisions.get(rec.key, {}).get("repeat_ok"):
                item.warnings.append(why.split(":")[0] + " (accepted)")
            else:
                p.issues.append(rv.Issue(rec.key, "repeat", f"{rec.label}: {why}"))

    def round_plan(self) -> tuple[rv.Plan, set]:
        """(the plan for the ready sheets only, keys held back). A child with any open sheet waits whole."""
        cp, _ = self.previous()
        decisions = self.decisions()
        records = self.records()
        back: set = set()
        for _ in range(20):
            p = self._plan([r for r in records if r.key not in back], cp, decisions)
            if p.ready:
                return p, back
            open_keys = {i.key for i in p.issues}
            ids = {i.student_id or (rv.NEW + i.record.key if i.new_student else None)
                   for i in p.settled if i.record.key in open_keys} - {None}
            back |= open_keys | {i.record.key for i in p.settled
                                 if i.student_id in ids or (i.student_id or "").removeprefix(rv.NEW) in open_keys}
        raise ck.CheckpointError("could not separate the ready sheets from the open ones")

    def counts(self) -> dict:
        """How many sheets are open, ready, held, skipped (ready but not counted) and already printed."""
        full = self.plan()
        rp, back = self.round_plan()
        open_keys = {i.key for i in full.issues}
        filed = self.log.state().filed
        return {"open": len(open_keys), "waiting": len(back - open_keys),
                "ready": sum(1 for i in rp.settled if not i.skipped or i.merged),
                "children": len({rp.resolved.get(i.record.key) or i.student_id or i.record.key
                                 for i in rp.settled if not i.skipped}),
                "skipped": sum(1 for i in rp.settled if i.skipped and not i.merged),
                "held": len(self.held()),
                "printed": sum(1 for r in self.all_records() if r.key in filed)}

    # ---- review (held sheets can be decided too)
    def _known(self) -> set:
        return {r.key for r in self.unfiled()}

    def decide(self, key: str, **decision) -> None:
        if key not in self._known():
            raise ck.CheckpointError(f"no sheet {key!r} waiting in the inbox")
        decisions = self.decisions()
        decisions.setdefault(key, {}).update(decision)
        rv.save_decisions(decisions, self.decisions_path, "command line")

    def set_decision(self, key: str, decision: dict, source: str = "review screen") -> None:
        if key not in self._known():
            raise ck.CheckpointError(f"no sheet {key!r} waiting in the inbox")
        decisions = self.decisions()
        if decision:
            decisions[key] = decision
        else:
            decisions.pop(key, None)
        rv.save_decisions(decisions, self.decisions_path, source)

    # ---- a round
    def next_round_number(self, month: str, state=None) -> int:
        st = state or self.log.state()
        used = [e.data.get("round", 1) for e in st.rounds if e.data["month"] == month]
        n = max(used, default=0) + 1
        while self.ws.round_folder(month, n).path.exists():
            n += 1
        return n

    def finish(self, who: str = "") -> str:
        """Print a round: every ready sheet, for the round's month. Returns the summary text."""
        if not who.strip():
            raise ck.CheckpointError("add your name in Settings first: it is recorded on every change")
        month = self.round_month
        latest = self.latest_round_month()
        if latest and month < latest:
            raise ck.CheckpointError(f"{ck.month_label(latest)} has already been printed; choose that month or a later one")
        p, back = self.round_plan()
        if not any(not i.skipped for i in p.settled):
            raise ck.CheckpointError("nothing is ready to print yet")
        n = self.next_round_number(month)
        out = self.ws.round_folder(month, n)
        keys = sorted(i.record.key for i in p.settled)
        text = self._write(p, out, month, who, [i.record for i in p.settled],
                           f"{ck.month_label(month)}, round {n}", {"round": n, "keys": keys})
        self.save_settings(round_month="")              # the next round starts from the default again
        return text

    def undo_round(self, batch: str, who: str, reason: str) -> str:
        """Cancel the latest round (its events, voided with the reason) and move its files aside. Its sheets go back
        to the inbox with their decisions."""
        import datetime as dt
        import shutil
        if not who.strip():
            raise ck.CheckpointError("add your name in Settings first: it is recorded on every change")
        if not reason.strip():
            raise ck.CheckpointError("say why: the reason is kept in the history")
        st = self.log.state()
        if not st.rounds or st.rounds[-1].batch != batch:
            raise ck.CheckpointError("only the latest round can be undone (later rounds started from its totals)")
        e = st.rounds[-1]
        targets = [x for x in st.events if x.batch == batch and x.id not in st.voided]
        n = e.data.get("round", 1)
        why = f"undid {ck.month_label(e.data['month'])} round {n}: {reason.strip()}"
        self.log.append([ev.Event("event_voided", {"event": x.id}, who, why, ({"manual": True},)) for x in targets])
        folder = self.ws.round_folder(e.data["month"], n).path
        if folder.exists():
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H.%M.%S")
            shutil.move(str(folder), folder.with_name(f"{folder.name} (undone {stamp})"))
        return why


def reprint_map(state: ev.State, results, month: str, table, exclude: str = "") -> dict:
    """{student id: (the month's pages including these, every level reached in the month)} for children who already
    had pages for `month` before these results (`state` is the log before them; `exclude`: a batch to leave out)."""
    earlier: dict = {}
    for e in state.events:
        if (e.kind in ("pages_recorded", "pages_adjusted") and e.id not in state.voided and e.data["month"] == month
                and (not exclude or e.batch != exclude)):
            earlier[e.student] = earlier.get(e.student, 0) + e.data.get("pages", e.data.get("delta", 0))
    out = {}
    for r in results:
        before = earlier.get(r.student.id, 0)
        if before > 0:
            pages = before + r.pages
            out[r.student.id] = (pages, ck.prizes_crossed(r.student.total - pages, r.student.total, table))
    return out


def round_folder_for(ws: wsp.Workspace, e: ev.Event) -> wsp.RoundFolder:
    return ws.round_folder(e.data["month"], e.data.get("round", 1))


def move_to_inbox(ws: wsp.Workspace, who: str) -> list[str]:
    """One time: move every month folder (2026-27/2026-10/...) into the inbox. Scans, photos, form downloads,
    readings and decisions join the inbox; a finished month's files become '<month> round 1', and the sheets it
    used are recorded as filed (an event in the same batch as its finish, so undoing that round frees them)."""
    import json
    import shutil
    from rc import forms
    log = ev.EventLog(ws.root)
    if not log.has_students():
        raise ck.CheckpointError("this data folder has no event log yet")
    if not who.strip():
        raise ck.CheckpointError("say who is doing this (--who)")
    st = log.state()
    pool = Pool(ws)
    inbox = ws.inbox().create()
    months = sorted((s, m) for s in ws.seasons() for m in
                    (p.name for p in (ws.root / s).iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}", p.name)))
    if not months:
        return ["no month folders to move"]
    records = {r.key: r for r in (rv.load_records(pool.records_path) if pool.records_path.exists() else [])}
    decisions = pool.decisions()
    done = []
    for season, month in months:
        mf = ws.month_folder(month, season)
        recs = rv.load_records(mf.work / RECORDS) if (mf.work / RECORDS).exists() else []
        form_recs = [r for f in mf.form_files() for r in forms.records(f, month)[0]] if mf.scans.is_dir() else []
        mdec = rv.load_decisions(mf.work / DECISIONS)
        clash = [k for k in mdec if k in decisions and decisions[k] != mdec[k]]
        if clash:
            raise ck.CheckpointError(f"{ck.month_label(month)}: sheet {clash[0]} was decided differently in the inbox")
        for src in sorted(mf.scans.rglob("*")) if mf.scans.is_dir() else []:
            if src.is_dir() or src.name.startswith("."):
                continue
            dest = inbox.scans / src.relative_to(mf.scans)
            if dest.exists():
                if dest.read_bytes() == src.read_bytes() or src.suffix.lower() in wsp.FORM_EXTS:
                    if src.suffix.lower() in wsp.FORM_EXTS and src.stat().st_mtime > dest.stat().st_mtime:
                        dest.unlink()                                  # keep the newer download of the responses
                    else:
                        src.unlink()
                        continue
                else:
                    raise ck.CheckpointError(f"{dest.name} is already in the inbox and is a different file; rename one")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
        raw = mf.work / "raw"
        if raw.is_dir():
            (inbox.work / "raw").mkdir(parents=True, exist_ok=True)
            for src in raw.glob("*"):
                dest = inbox.work / "raw" / src.name
                if not dest.exists():
                    shutil.move(str(src), dest)
        for r in recs:
            records.setdefault(r.key, r)
        decisions.update(mdec)
        if (mf.work / "decisions.log").exists():
            with open(inbox.work / "decisions.log", "a", encoding="utf-8") as fh:
                fh.write((mf.work / "decisions.log").read_text(encoding="utf-8"))
        merged_files = [mf.work / RECORDS, mf.work / DECISIONS, mf.work / "decisions.log"]
        fin = st.finished.get(month)
        if fin is not None:
            out = ws.round_folder(month, fin.data.get("round", 1))
            if out.path.exists():
                raise ck.CheckpointError(f"{out.path} already exists")
            out.create()
            for item in sorted(mf.path.iterdir()):
                if item.name not in ("scans", "work") and not item.name.startswith("."):
                    shutil.move(str(item), out.path / item.name)
            if (mf.work / "removed.txt").exists():
                shutil.move(str(mf.work / "removed.txt"), out.path / "removed.txt")
            keys = sorted({r.key for r in recs} | {r.key for r in form_recs})
            log.append([ev.Event("sheets_filed", {"month": month, "keys": keys}, who,
                                 f"moved {ck.month_label(month)} into the inbox: its finish used these sheets",
                                 ({"manual": True},), batch=fin.batch)])
            done.append(f"{ck.month_label(month)}: {len(keys)} sheets filed; files now in {out.path.relative_to(ws.root)}")
        else:
            done.append(f"{ck.month_label(month)}: not finished; its sheets wait in the inbox")
        rv.save_records(list(records.values()), pool.records_path)       # saved before the month's copies go
        rv.save_decisions(decisions, pool.decisions_path, f"moved {ck.month_label(month)} into the inbox")
        for f in merged_files:
            f.unlink(missing_ok=True)
        # forget the month folder once it is empty (anything unexpected stays, and is reported)
        for d in sorted((x for x in mf.path.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)) + [mf.path]:
            try:
                for junk in d.glob(".DS_Store"):
                    junk.unlink()
                d.rmdir()
            except OSError:
                pass
        if mf.path.exists():
            done.append(f"  left behind in {mf.path.relative_to(ws.root)}: "
                        + ", ".join(str(x.relative_to(mf.path)) for x in mf.path.rglob("*") if x.is_file()))
    if not pool.settings().get("forms_from"):
        pool.save_settings(forms_from=months[0][1])
    return done


def reprint(m: "Month", batch: str = "") -> list[str]:
    """Redraw a finished month's (or a round's, by its batch id) certificates and class lists from the event log
    (no events change): for a new layout, or a lost file. Pages, totals and prizes are exactly what was recorded."""
    state = m.log.state()
    if batch:
        fin = next((e for e in state.rounds if e.batch == batch), None)
        if fin is None:
            raise ck.CheckpointError("no such round (or it was undone)")
        month, out = fin.data["month"], round_folder_for(m.ws, fin)
    else:
        month, out = m.folder.month, m.folder
        fin = state.finished.get(month)
    if fin is None:
        raise ck.CheckpointError(f"{ck.month_label(month)} is not finished in the event log")
    batch = [e for e in state.events if e.batch == fin.batch and e.id not in state.voided]
    pages = {e.student: e.data["pages"] for e in batch if e.kind == "pages_recorded"}
    given = {}
    owed_levels = {}
    for e in batch:
        if e.kind == "prizes_given":
            target = owed_levels if "owed from earlier" in e.reason else given
            target.setdefault(e.student, []).extend(e.data["levels"])
    after = {}
    for h in state.history:                      # each child's total right after this month's pages
        if h.month == month and h.student_id in pages:
            after[h.student_id] = h.total_after
    level = {t: lv for lv in m.table for t in [lv[0]]}
    results = []
    for sid, n in pages.items():
        s = ck.replace(state.students[sid], total=after.get(sid, state.students[sid].total))
        results.append(ck.Result(s, n, s.total - n, tuple(level[t] for t in sorted(given.get(sid, [])) if t in level)))
    owed_rows = [(state.students[sid], [level[t] for t in sorted(lv) if t in level]) for sid, lv in owed_levels.items()]
    label = ck.month_label(month)
    warnings: list[str] = []
    before = ev.replay([e for e in state.events if e.time < fin.time])
    reprints = reprint_map(before, results, month, m.table)
    out.create()
    wsp.save_atomic(lambda tmp: warnings.extend(render.render_print_file(results, label, tmp, m.table, owed_rows,
                                                                         reprints)),
                    out.certificates)
    if out.class_lists.exists():               # the lists used to be a separate file; now they're page 1
        out.class_lists.unlink()
    return warnings


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
                 who: str, owed: dict | None = None, teacher_grades: dict | None = None,
                 finish_data: dict | None = None) -> list[ev.Event]:
    """What finishing a month adds to the log, each change pointing at the sheet(s) it came from. All share one
    batch id (the month_finished event's), so reopening the month cancels exactly these."""
    import datetime as dt
    start = dt.datetime.now().astimezone()
    # events apply in time order, so each gets its own moment, in the order written (a new student before
    # their pages)
    tick = lambda: (start + dt.timedelta(microseconds=len(out))).isoformat(timespec="microseconds")
    out: list[ev.Event] = []
    grades = teacher_grades or {}
    finish = ev.Event("month_finished", {"month": month, "sheets": len(p.settled), **(finish_data or {})}, who, "", (),
                      tick())
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
            grade = s.grade or grades.get(s.teacher, "")          # the teacher's grade when the sheet doesn't say
            mk("student_added", {"student": sid, "name": s.name, "teacher": s.teacher, "grade": grade},
               [src_of(r) for r in (item.sources or [rec])], "new this month")
            added.add(sid)
        if item.set_teacher and sid in before.students:
            mk("teacher_set", {"student": sid, "teacher": item.set_teacher}, [src_of(rec)],
               f"sheet says {rec.teacher!r}")
            new_grade = grades.get(item.set_teacher, "")           # a class change brings the class's grade
            if new_grade and new_grade != before.students[sid].grade:
                mk("grade_set", {"student": sid, "grade": new_grade}, [src_of(rec)],
                   f"moved to {item.set_teacher}'s class (grade {new_grade})")
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
