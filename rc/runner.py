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

    # ---- inputs
    def previous(self) -> tuple[ck.Checkpoint, str]:
        path, why = self.ws.previous_checkpoint(self.folder.month, self.folder.season)
        return (ck.load(path) if path else ck.Checkpoint()), why

    def records(self) -> list[rv.SheetRecord]:
        if not self.records_path.exists():
            raise ck.CheckpointError(f"no sheets have been read yet: {self.records_path} is missing "
                                     f"(scans go in {self.folder.scans})")
        return rv.load_records(self.records_path)

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
    def finish(self) -> str:
        """Write certificates.pdf, summary.txt and (last) checkpoint.xlsx. Returns the summary text."""
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

        self.folder.create()
        warnings: list[str] = []
        wsp.save_atomic(lambda tmp: warnings.extend(render.render_print_pdf(results, label, tmp, self.table)),
                        self.folder.certificates)
        text = summary(label, why, before, new_cp, results, notes, warnings, self.table, audit.report(checked, spots))
        wsp.write_atomic(self.folder.summary, text)
        wsp.save_atomic(lambda tmp: ck.save(new_cp, tmp, results, self.table,
                                            sheets=(audit.SHEET_COLS, [r.cells() for r in rows])),
                        self.folder.checkpoint)
        return text


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


def summary(label, source, before, after, results, notes, warnings, table, checks=()) -> str:
    new_ids = set(after.students) - set(before.students)
    prizes = Counter()
    for r in results:
        for p in r.prizes:
            prizes[p[2]] += 1
    absent = ck.not_seen(after, results)
    out = [f"Reading Challenge - {label}", "=" * 40, "",
           f"Started from: {source}",
           f"Certificates: {len(results)}   Classrooms: {len(ck.classrooms(results))}   "
           f"New students: {len(new_ids)}   Pages read: {sum(r.pages for r in results):,}", ""]
    if checks:
        out += list(checks) + [""]
    out.append("Prizes to hand out" + ("" if prizes else ": none"))
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
