"""Starting checkpoint from the master WORKBOOK (.xlsx), not a printout: totals as of the end of one month.

The workbook has a roster tab (PRIMARY: name, teacher, grade, a running total) and one tab per month
("Oct 2021" ... "September 2026") listing the children who turned something in: previous + this month =
grand total, one row per sheet (a child with two sheets has two chained rows). Each tab ends with a
prize-stock table, which is not children and is skipped.

PRIMARY's running total can be ahead of or behind the month tabs (a month entered early, or a month's pages
not copied over), so the starting total for each child is their end-of-month total on the LAST MONTH TAB
up to `through` that lists them; PRIMARY's own number is used only for children on no month tab. Every
disagreement is reported and noted on the student, so a person can check it against the sheets.

    python -m rc --data D import-master master.xlsx --through "September 2026" --season 2026-27

Children on month tabs but not on PRIMARY (archived, left) are not imported. Real names: keep the
workbook and its output out of git.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rc import checkpoint as ck
from rc import matching as mt

ROSTER_TAB = "PRIMARY"
NOT_MONTHS = re.compile(r"primary|prize|sheet\d|archived", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], start=1)}
RED = {"FFFF0000", "00FF0000"}


def tab_month(title: str) -> str | None:
    """'Oct 2021' -> '2021-10'; 'FEB-2 2022' -> '2022-02'; 'Summer 2026' -> '2026-07'; other tabs -> None."""
    if NOT_MONTHS.search(title):
        return None
    word, year = re.match(r"\s*([A-Za-z]*)", title).group(1).lower(), re.search(r"(?<!\d)(\d{4})(?!\d)", title)
    if not word or not year:
        return None
    year = int(year.group(1))
    if word.startswith("summer"):
        return f"{year}-07"
    month = MONTHS.get(word[:3])
    return f"{year}-{month:02d}" if month else None


def _num(v) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    s = str(v).replace(",", "").strip()
    return int(float(s)) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def _grade(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v or "").strip()


def _key(name: str) -> str:
    return mt.normalize(name)


@dataclass
class MonthRow:
    name: str
    teacher: str
    prev: int | None
    cur: int | None
    grand: int | None
    row: int = 0                # row number on its tab (for pointing back at it)


def _columns(header) -> dict:
    h = [str(x or "").strip().lower() for x in header]

    def find(*starts):
        return next((i for i, x in enumerate(h) if x.startswith(starts)), None)
    return {"teacher": 1, "grade": next((i for i, x in enumerate(h) if "grade" in x), None),
            "prev": find("previous total"), "cur": find("current month total"), "grand": find("grand total")}


def _rows(ws):
    """(cells, row number) for the children on a tab; stops at the prize-stock table under them."""
    rows = ws.iter_rows()
    header = [c.value for c in next(rows)]
    for row in rows:
        cells = [c.value for c in row]
        name = str(cells[0] or "").strip()
        second = str(cells[1] or "").strip().lower() if len(cells) > 1 else ""
        if name.lower() == "prize" and second == "level":
            break                                   # the prize-stock table (a staff reader's "Teacher" row is not)
        if name:
            yield row, cells
    return header


def month_end(rows: list[MonthRow]) -> tuple[int | None, int, str]:
    """(total at the end of the month, pages that month, problem) for one child's rows on one month tab.

    Several rows are several sheets, normally chained (each previous = the last grand). If they are not, the
    largest grand total is used and the problem is described."""
    totals = []
    for r in rows:
        g = r.grand if r.grand is not None else (r.prev + r.cur if None not in (r.prev, r.cur) else r.prev)
        if g is not None:
            totals.append((g, r))
    if not totals:
        return None, 0, ""
    end = max(g for g, _ in totals)
    start = min((r.prev for _, r in totals if r.prev is not None), default=end)
    problem = ""
    if len(rows) > 1:
        chain = sorted(totals, key=lambda t: t[0])
        broken = any(b.prev != a_g for (a_g, _), (_, b) in zip(chain, chain[1:]))
        if broken:
            problem = "rows that do not chain: " + "; ".join(
                f"{r.prev}+{r.cur}={g}" + (f" ({r.teacher})" if r.teacher else "") for g, r in chain)
    return end, end - start, problem


def parse_workbook(path, through: str, teacher_aliases=None, keep_ids: ck.Checkpoint | None = None):
    """(checkpoint as of the end of `through`, report lines)."""
    children, report, _ = _read(path, through, teacher_aliases, keep_ids)
    cp = ck.Checkpoint(month=ck.month_key(through))
    for c in children:
        cp.students[c["id"]] = ck.Student(c["id"], c["name"], c["teacher"], c["grade"], c["total"],
                                          notes="; ".join(c["notes"]))
        for title, month, start, end, rows, _ in c["history"]:
            cp.history.append(ck.HistoryRow(month, c["id"], end - start, end, f"master: {title}"))
    return cp, report


def opening_events(path, through: str, who: str, teacher_aliases=None, keep_ids: ck.Checkpoint | None = None):
    """(events, report): the workbook as the first events in the log, every child's month-by-month history
    back to the first tab, each event pointing at its tab and row. Where the tabs do not add up (a child who
    arrives with a total, a gap between tabs, PRIMARY disagreeing) a `total_set` says so, with the reason.

    Times are the months the reading happened (the 28th, noon), so replaying puts history in order and
    before anything recorded live. Prizes up to each child's opening total are recorded as handed out."""
    from rc import events as ev
    import make_certificates as mc
    import datetime as dt
    children, report, meta = _read(path, through, teacher_aliases, keep_ids)
    wb_name = Path(path).name
    through = ck.month_key(through)
    table = mc.load_levels()

    def at(month, step=0, tab_index=0):
        y, m = (int(x) for x in month.split("-"))
        t = dt.datetime(y, m, 28, 12, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=tab_index, seconds=step)
        return t.isoformat(timespec="seconds")

    def src(tab, rows):
        return tuple({"workbook": wb_name, "tab": tab, "row": r} for r in rows)

    out = []
    end_time = at(through, 59, 59)
    for c in children:
        sid = c["id"]
        first = c["history"][0][1] if c["history"] else through
        out.append(ev.Event("student_added", {"student": sid, "name": c["name"], "teacher": c["teacher"],
                                              "grade": c["grade"]}, who, "", src(ROSTER_TAB, [c["row"]]),
                            at(first, 0, -1)))
        running = 0
        for title, month, start, end, rows, i in c["history"]:
            if start != running:
                why = (f"arrives in {title} with {start:,} pages already" if running == 0 else
                       f"{title} starts at {start:,} but the previous tab ended at {running:,}")
                out.append(ev.Event("total_set", {"student": sid, "total": start}, who, why, src(title, rows),
                                    at(month, 0, i)))
            if end != start:
                out.append(ev.Event("pages_recorded", {"student": sid, "month": month, "pages": end - start}, who,
                                    "", src(title, rows), at(month, 1, i)))
            running = end
        if c["total"] != running:
            why = "; ".join(n for n in c["notes"] if "PRIMARY said" in n) or \
                f"{ROSTER_TAB} total (on no month tab)"
            out.append(ev.Event("total_set", {"student": sid, "total": c["total"]}, who, why,
                                src(ROSTER_TAB, [c["row"]]), end_time))
        for note in c["notes"]:
            out.append(ev.Event("note_added", {"student": sid, "text": note}, who, "", src(ROSTER_TAB, [c["row"]]),
                                end_time))
        given = [t for t, _, _ in table if t <= c["total"]]
        if given:
            out.append(ev.Event("prizes_given", {"student": sid, "month": through, "levels": given}, who,
                                "assumed handed out before the app (from the workbook)", (), end_time))
    return out, report


def _read(path, through, teacher_aliases, keep_ids):
    """[child dicts], report lines, meta: everything both views of the import need."""
    import openpyxl
    through = ck.month_key(through)
    aliases = {k.strip().lower(): v.strip() for k, v in (teacher_aliases or {}).items()}
    wb = openpyxl.load_workbook(path, data_only=True)
    if ROSTER_TAB not in wb.sheetnames:
        raise ck.CheckpointError(f"{path}: no {ROSTER_TAB!r} tab (the roster)")

    # month tabs, in workbook order (which is time order), up to and including `through`
    month_tabs = [(t, tab_month(t)) for t in wb.sheetnames]
    month_tabs = [(t, k) for t, k in month_tabs if k]
    used = [(t, k) for t, k in month_tabs if k <= through]
    later = [(t, k) for t, k in month_tabs if k > through]
    if not any(k == through for _, k in used):
        raise ck.CheckpointError(f"no tab for {ck.month_label(through)}; month tabs are: "
                                 + ", ".join(t for t, _ in month_tabs))

    def read_tab(title):
        ws = wb[title]
        gen = _rows(ws)
        header = [c.value for c in ws[1]]
        cols = _columns(header)
        out: dict[str, list[MonthRow]] = {}
        for row, cells in gen:
            get = lambda i: cells[i] if i is not None and i < len(cells) else None
            out.setdefault(_key(str(cells[0])), []).append(
                MonthRow(str(cells[0]).strip(), str(get(1) or "").strip(), _num(get(cols["prev"])),
                         _num(get(cols["cur"])), _num(get(cols["grand"])), row[0].row))
        return out

    tabs = [(t, k, read_tab(t)) for t, k in used]
    later_tabs = [(t, k, read_tab(t)) for t, k in later]

    # roster
    ws = wb[ROSTER_TAB]
    cols = _columns([c.value for c in ws[1]])
    roster = []
    for row, cells in _rows(ws):
        red = bool(row[0].font and row[0].font.color is not None and row[0].font.color.rgb in RED)
        teacher = str(cells[1] or "").strip()
        roster.append((str(cells[0]).strip(), aliases.get(teacher.lower(), teacher),
                       _grade(cells[cols["grade"]] if cols["grade"] is not None else ""),
                       _num(cells[cols["prev"]]), red, row[0].row))

    cp = ck.Checkpoint(month=through)              # only to hand out ids
    children = []
    old_ids = {}
    if keep_ids is not None:
        for s in keep_ids.students.values():
            old_ids.setdefault(_key(s.name), []).append(s.id)
    report, seen = [], {}
    through_title = next(t for t, k in used if k == through)
    for name, teacher, grade, primary, red, prow in roster:
        key = _key(name)
        if key in seen:
            report.append(f"{name}: on {ROSTER_TAB} twice; only the first row is used")
            continue
        notes, total, history, source = [], None, [], ""
        for i, (title, month, tab) in enumerate(tabs):
            if key in tab:
                end, pages, problem = month_end(tab[key])
                if end is None:
                    continue
                total, source = end, title
                history.append((title, month, end - pages, end, [r.row for r in tab[key]], i))
                if problem and month == through:
                    notes.append(f"{title}: {problem}")
        if total is None:
            total = primary or 0
            if primary is None:
                notes.append(f"no total anywhere; started at 0")
        elif primary is not None and primary != total:
            ahead = next((t for t, _, tab in later_tabs if key in tab and month_end(tab[key])[0] == primary), None)
            if ahead:
                notes.append(f"{ROSTER_TAB} said {primary:,}, which already includes {ahead} "
                             f"({primary - total:+,}); started from the end of {source} ({total:,})")
            else:
                notes.append(f"{ROSTER_TAB} said {primary:,} but {source} ends at {total:,}; used {total:,} - check")
        if red:
            notes.append(f"red in {ROSTER_TAB}: no recent sheets")
        if not teacher:
            report.append(f"{name}: no teacher in {ROSTER_TAB}")
        ids = old_ids.get(key, [])
        sid = ids[0] if len(ids) == 1 and ids[0] not in cp.students else None
        sid = sid or _next_free(cp, keep_ids)
        cp.students[sid] = ck.Student(sid, name, teacher)
        seen[key] = sid
        children.append({"id": sid, "name": name, "teacher": teacher, "grade": grade, "total": total,
                         "notes": notes, "history": history, "row": prow, "primary": primary})
        for n in notes:
            if "check" in n or "do not chain" in n or "already includes" in n:
                report.append(f"{name}: {n}")
    on_tabs = {k for _, _, tab in tabs for k in tab}
    gone = len(on_tabs - set(seen))
    if gone:
        report.append(f"{gone} children on month tabs are not on {ROSTER_TAB} (archived or left); not imported")
    report.insert(0, f"Totals as of the end of {through_title}, from {len(tabs)} month tabs "
                     f"({tabs[0][0]} to {through_title}); {len(cp.students)} students")
    return children, report, {"through_title": through_title}


def _next_free(cp: ck.Checkpoint, keep: ck.Checkpoint | None) -> str:
    taken = set(cp.students) | (set(keep.students) if keep else set())
    nums = [int(m.group(1)) for sid in taken if (m := re.fullmatch(r"S(\d+)", sid))]
    return f"S{(max(nums) + 1 if nums else 1):04d}"
