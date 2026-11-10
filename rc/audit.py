"""Checking a finished month without reading the code: a row per scanned page, and arithmetic re-done from scratch.

`sheet_rows()` traces every scan page to what happened to it (counted, added to another sheet, replaced,
skipped) and, for the child it counted for, the previous total, new total and prizes. It becomes the
"Sheets" tab of checkpoint.xlsx, so a person can hold any certificate up against its scan.

`checks()` recomputes everything from the saved numbers, independently of how they were produced:
every page accounted for, new total = previous + this month, children without a sheet unchanged, prizes
exactly the levels crossed. They check the bookkeeping; whether a number was READ right is what the
spot-check list (`spot_checks()`) is for: a person compares those rows with the scans.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from rc import checkpoint as ck
from rc import review as rv

LOTS_OF_PAGES = 5000      # a month this big is worth a look (most children read under 2,000)

SHEET_COLS = ["Scan page", "Name on sheet", "Teacher on sheet", "Pages on sheet", "Pages used", "Why different",
              "What happened", "Counted for", "Class", "Month total", "Previous total", "New total", "Prizes"]


def _short(label: str) -> str:
    return label.rsplit(" ", 2)[-2] + " " + label.rsplit(" ", 1)[-1] if " p. " in label else label   # "p. 3"


@dataclass
class Row:
    page: str
    name_read: str
    teacher_read: str
    pages_read: int | None
    pages_used: int | None
    why_different: str
    happened: str
    student: str = ""
    teacher: str = ""
    month_total: int | None = None
    previous: int | None = None
    new_total: int | None = None
    prizes: str = ""
    result: ck.Result | None = None

    def cells(self) -> list:
        return [self.page, self.name_read, self.teacher_read, self.pages_read, self.pages_used, self.why_different,
                self.happened, self.student, self.teacher, self.month_total, self.previous, self.new_total, self.prizes]


def sheet_rows(plan: rv.Plan, decisions: dict, results: list[ck.Result]) -> list[Row]:
    by_label = {}
    for r in results:
        for label in r.source.split(", "):
            by_label[label] = r
    rows = []
    for item in plan.settled:
        rec = item.record
        d = decisions.get(rec.key, {})
        used = item.own_pages if not (item.skipped and not item.merged) else None
        why = ""
        if used is not None and used != rec.pages:
            why = "typed in review" if "pages" in d else "changed in review"
        elif used is not None and rec.pages_confidence != rv.HIGH:
            why = "read unsure; confirmed in review"
        labels = [s.label for s in item.sources] if item.sources else [rec.label]
        if item.skipped and not item.merged:
            happened = "skipped: " + item.skipped
        elif item.merged:
            happened = item.skipped
        elif rec.label not in labels:
            happened = "replaced by " + ", ".join(_short(x) for x in labels)
        elif len(labels) > 1:
            happened = "counted, with " + ", ".join(_short(x) for x in labels if x != rec.label)
        else:
            happened = "counted"
        res = by_label.get(rec.label) or (by_label.get(labels[0]) if not item.skipped else None)
        if item.merged:
            res = by_label.get(rec.label) or res
        row = Row(_short(rec.label), rec.name, rec.teacher, rec.pages, used, why, happened, result=res)
        if res:
            row.student, row.teacher = res.student.name, res.student.teacher
            row.month_total, row.previous, row.new_total = res.pages, res.previous_total, res.student.total
            row.prizes = "; ".join(f"{p[1]}: {p[2]}" for p in res.prizes)
        rows.append(row)
    return rows


def checks(records, plan: rv.Plan, start: ck.Checkpoint, with_new: ck.Checkpoint, after: ck.Checkpoint,
           results: list[ck.Result], table) -> list[tuple[bool, str]]:
    """[(passed, what was checked)]. `start` is last month's checkpoint, `with_new` the same plus this
    month's new students, `after` the new checkpoint."""
    out = []
    counted = sum(1 for i in plan.settled if not i.skipped)
    merged = sum(1 for i in plan.settled if i.merged)
    skipped = sum(1 for i in plan.settled if i.skipped and not i.merged)
    out.append((counted + merged + skipped == len(records) == len(plan.settled),
                f"every scan page accounted for: {len(records)} pages = {counted} counted + {merged} added to or "
                f"replacing another sheet + {skipped} skipped"))
    out.append((counted == len(results), f"one certificate per counted child: {counted} counted, "
                                         f"{len(results)} certificates"))
    bad = [r.student.name for r in results if r.student.total != r.previous_total + r.pages]
    out.append((not bad, "new total = previous total + this month's pages, for every child with a sheet"
                + (f" (wrong for: {', '.join(bad)})" if bad else "")))
    bad = [r.student.name for r in results
           if r.student.id in start.students and start.students[r.student.id].total != r.previous_total]
    out.append((not bad, "previous totals are last month's totals" + (f" (wrong for: {', '.join(bad)})" if bad else "")))
    seen = {r.student.id for r in results}
    bad = [s.name for sid, s in start.students.items()
           if sid not in seen and (sid not in after.students or after.students[sid].total != s.total)]
    out.append((not bad, f"{len(start.students) - len(seen & set(start.students))} children without a sheet "
                         f"carried forward unchanged" + (f" (changed: {', '.join(bad)})" if bad else "")))
    added = sum(r.pages for r in results)
    grew = sum(s.total for s in after.students.values()) - sum(s.total for s in with_new.students.values())
    out.append((added == grew, f"pages this month ({added:,}) = growth of all totals together ({grew:,})"))
    bad = [r.student.name for r in results if r.prizes != ck.prizes_crossed(r.previous_total, r.student.total, table)]
    out.append((not bad, "prizes are exactly the levels between each child's previous and new total"
                + (f" (wrong for: {', '.join(bad)})" if bad else "")))
    new_ids = set(after.students) - set(start.students)
    out.append((len(new_ids) == len(set(with_new.students) - set(start.students)),
                f"{len(new_ids)} new students, each added once"))
    return out


def spot_checks(rows: list[Row], month: str, how_many: int = 5) -> list[tuple[str, list[Row]]]:
    """Rows worth holding up against the scans, grouped by why."""
    counted = [r for r in rows if r.result]
    rng = random.Random(month)                    # the same picks every time for the same month
    picks = rng.sample(counted, min(how_many, len(counted)))
    changed = [r for r in rows if r.why_different.endswith("in review") and not r.why_different.startswith("read")
               or r.happened.startswith(("added", "replaces", "replaced"))]
    big = sorted((r for r in counted if r.result.prizes), key=lambda r: (-len(r.result.prizes), -r.month_total))[:5]
    many = [r for r in counted if r.month_total and r.month_total >= LOTS_OF_PAGES]
    return [("random sample", picks), ("pages changed, added or replaced in review", changed),
            ("most prizes", big), (f"{LOTS_OF_PAGES:,} or more pages in one month (misread, or a running total?)", many)]


def report(check_results, spots) -> list[str]:
    """Lines for summary.txt."""
    failed = [t for ok, t in check_results if not ok]
    out = ["Checks" + (": all passed" if not failed else f": {len(failed)} FAILED - do not print yet")]
    out += [f"  {'ok  ' if ok else 'FAIL'}  {t}" for ok, t in check_results]
    out += ["", "Spot-check against the scans (Sheets tab in checkpoint.xlsx has every page)"]
    seen = set()
    for why, rows in spots:
        rows = [r for r in rows if (why, r.page) not in seen]
        if not rows:
            continue
        out.append(f"  {why}:")
        for r in rows:
            seen.add((why, r.page))
            used = f"{r.pages_used:,}" if r.pages_used is not None else "-"
            who = f"{r.student} ({r.teacher})" if r.student else "-"
            out.append(f"    {r.page:<7} sheet says {r.name_read or '?'!r}, {used} pages -> {who}"
                       + (f"; {r.happened}" if r.happened != "counted" else "")
                       + (f"; {r.why_different}" if r.why_different else ""))
    return out
