"""Turn CSV exports of the tracker workbook (Entries, Prizes, Roster tabs) into certificate rows.

Rules (they mirror the formulas in tracker_template.xlsx):
  * Void rows are ignored.
  * For one child and month, the LAST entry row wins (a correction is a new row).
  * An entry counts only once it has a Child ID that exists on the Roster, plus a month and pages.
    The others are returned as `unresolved` so they get chased, never silently dropped.
  * Total pages "as of" a month is the sum of the counted months up to and including it.
  * Prizes owed = every level with  last_prize_given < threshold <= total  (the highest
    threshold on a non-void Prizes row is "last given").
"""
import csv
import re
from datetime import datetime


def to_int(text):
    return int(re.sub(r"[,\s]", "", text))


def month_key(text):
    """'October 2026' or '2026-10' -> '2026-10'."""
    text = text.strip()
    for fmt in ("%Y-%m", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m")
        except ValueError:
            pass
    raise ValueError(f"cannot read month {text!r}; use 'October 2026' or '2026-10'")


def month_label(key):
    return datetime.strptime(key, "%Y-%m").strftime("%B %Y")


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _need(rows, path, columns):
    have = set(rows[0].keys()) if rows else set(columns)
    missing = [c for c in columns if c not in have]
    if missing:
        raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")


def _blank(row, columns):
    """Empty rows in a sheet export still carry formula output (0, ""), so only the typed columns count."""
    return not any((row.get(c) or "").strip() for c in columns)


def load_roster(path):
    """{child_id: {"name", "teacher"}} from the Roster export."""
    rows = _read(path)
    _need(rows, path, ["Child ID", "Name", "Teacher"])
    return {r["Child ID"].strip(): {"name": r["Name"].strip(), "teacher": (r["Teacher"] or "").strip()}
            for r in rows if (r["Name"] or "").strip()}


def load_entries(path, roster):
    """Return (months, unresolved).
    months: {child_id: {month_key: pages}} after void/latest-wins.
    unresolved: [{"line", "month", "pages", "name_on_form", "teacher_on_form", "problem"}]"""
    rows = _read(path)
    _need(rows, path, ["Month", "Pages", "Name on form", "Teacher on form", "Child ID", "Void (x)"])
    months, unresolved = {}, []
    for line, r in enumerate(rows, start=2):
        if _blank(r, ["Month", "Pages", "Name on form", "Teacher on form", "Child"]) \
                or (r["Void (x)"] or "").strip():
            continue
        cid, month, pages = (r["Child ID"] or "").strip(), (r["Month"] or "").strip(), (r["Pages"] or "").strip()
        problem = None
        try:
            key = month_key(month) if month else None
            n = to_int(pages) if pages else None
        except ValueError:
            key = n = None
            problem = "month or pages not readable"
        if problem is None:
            if key is None or n is None:
                problem = "missing month or pages"
            elif not cid:
                problem = "no child chosen yet"
            elif cid not in roster:
                problem = f"child id {cid} is not on the roster"
        if problem:
            unresolved.append({"line": line, "month": month, "pages": pages,
                               "name_on_form": (r["Name on form"] or "").strip(),
                               "teacher_on_form": (r["Teacher on form"] or "").strip(),
                               "problem": problem})
            continue
        months.setdefault(cid, {})[key] = n  # later rows overwrite earlier ones
    return months, unresolved


def load_last_prize(path, roster):
    """Return ({child_id: highest threshold given}, problems)."""
    rows = _read(path)
    _need(rows, path, ["Child", "Level", "Threshold", "Child ID", "Void (x)"])
    last, problems = {}, []
    for line, r in enumerate(rows, start=2):
        if _blank(r, ["Child", "Level"]) or (r["Void (x)"] or "").strip():
            continue
        cid, thr = (r["Child ID"] or "").strip(), (r["Threshold"] or "").strip()
        if not cid or cid not in roster or not thr:
            problems.append(f"Prizes line {line}: {(r['Child'] or '').strip() or 'blank child'}"
                            f" - child or level not recognised, so it is not counted")
            continue
        last[cid] = max(last.get(cid, 0), to_int(thr))
    return last, problems


def build_rows(roster_csv, entries_csv, prizes_csv, month, table):
    """Certificate rows for `month` (anything month_key accepts).

    Returns (rows, notes, unresolved). Each row has name, pages, total_pages (as strings),
    teacher, child_id and `_resolved` = (level labels, prizes) for every level owed.
    `notes` lists children who are owed prizes but have no counted entry this month."""
    target = month_key(month)
    roster = load_roster(roster_csv)
    months, unresolved = load_entries(entries_csv, roster)
    last, prize_problems = load_last_prize(prizes_csv, roster)

    rows, notes = [], list(prize_problems)
    for cid, by_month in months.items():
        total = sum(p for m, p in by_month.items() if m <= target)
        owed = [(label, prize) for thr, label, prize in table if last.get(cid, 0) < thr <= total]
        if target in by_month:
            rows.append({"name": roster[cid]["name"], "teacher": roster[cid]["teacher"], "child_id": cid,
                         "pages": str(by_month[target]), "total_pages": str(total),
                         "_resolved": ([o[0] for o in owed], [o[1] for o in owed])})
        elif owed:
            notes.append(f"{roster[cid]['name']} ({roster[cid]['teacher']}) is owed "
                         f"{', '.join(o[0] for o in owed)} but has no entry for {month_label(target)}: no certificate made")
    rows.sort(key=lambda r: (r["teacher"].lower(), r["name"].lower()))
    return rows, notes, unresolved
