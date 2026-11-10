"""The audit: a row per scanned page, and the arithmetic re-checked from scratch. Fake children only."""
import sys
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import audit  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402

TABLE = mc.load_levels()


def rec(page, name, teacher, pages, **kw):
    return rv.SheetRecord("oct.pdf", page, name, teacher, "October 2026", pages, **kw)


RECORDS = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Ada Example", "Lopez", 20),
           rec(3, "Io Li", "Park", 150, pages_confidence=rv.LOW), rec(4, "Scribble", "", None),
           rec(5, "Zed", "Moren", 40), rec(6, "Zed", "Moren", 45)]
DECISIONS = {"oct.pdf#2": {"duplicate": "sum"}, "oct.pdf#3": {"pages": 160}, "oct.pdf#4": {"skip": "blank"},
             "oct.pdf#5": {"new_student": {"name": "Zed Newcomer", "teacher": "Moren"}},
             "oct.pdf#6": {"student": "new@oct.pdf#5", "duplicate": "replace"}}


@pytest.fixture
def month(tmp_path):
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)       # S0001
    ck.add_student(cp, "Io Li", "Park", "3", total=2400)            # S0002
    ck.add_student(cp, "Absent Al", "Lopez", "1", total=10)         # S0003
    start = tmp_path / "2026-27" / "start"
    start.mkdir(parents=True)
    ck.save(cp, start / "checkpoint.xlsx")
    m = runner.Month(wsp.Workspace(tmp_path), "October 2026")
    m.folder.create()
    rv.save_records(RECORDS, m.records_path)
    rv.save_decisions(DECISIONS, m.decisions_path)
    return m


def parts(m):
    start, _ = m.previous()
    p = m.plan()
    with_new, entries, _ = rv.finalize(p, start, TABLE)
    after, results = ck.apply_month(with_new, "2026-10", entries, TABLE)
    return start, p, with_new, after, results


def test_every_scan_page_gets_a_row_saying_what_happened(month):
    start, p, with_new, after, results = parts(month)
    rows = {r.page: r for r in audit.sheet_rows(p, DECISIONS, results)}
    assert list(rows) == [f"p. {i}" for i in range(1, 7)]
    assert rows["p. 1"].happened == "counted, with p. 2" and rows["p. 1"].month_total == 50
    assert (rows["p. 1"].previous, rows["p. 1"].new_total) == (74, 124)
    assert rows["p. 2"].happened.startswith("added to") and rows["p. 2"].pages_used == 20
    assert rows["p. 3"].pages_used == 160 and rows["p. 3"].why_different == "typed in review"
    assert "2,500" in rows["p. 3"].prizes                                    # 2,400 -> 2,560 crosses 2,500
    assert rows["p. 4"].happened == "skipped: blank" and rows["p. 4"].pages_used is None
    assert rows["p. 5"].happened == "replaced by p. 6" and rows["p. 5"].student == "Zed Newcomer"
    assert rows["p. 6"].happened.startswith("replaces") and rows["p. 6"].month_total == 45


def test_the_checks_pass_on_a_correct_month(month):
    start, p, with_new, after, results = parts(month)
    got = audit.checks(month.records(), p, start, with_new, after, results, TABLE)
    assert all(ok for ok, _ in got), [t for ok, t in got if not ok]
    text = " | ".join(t for _, t in got)
    assert "6 pages = 3 counted + 2 added to or replacing another sheet + 1 skipped" in text
    assert "1 children without a sheet carried forward unchanged" in text


def test_the_checks_catch_a_wrong_total_a_changed_absentee_and_a_wrong_prize(month):
    start, p, with_new, after, results = parts(month)
    ada = next(r for r in results if r.student.name == "Ada Example")
    bad_results = [ck.Result(ck.replace(ada.student, total=999), ada.pages, ada.previous_total, ada.prizes, ada.source)
                   if r is ada else r for r in results]
    io = next(r for r in bad_results if r.student.name == "Io Li")
    bad_results = [ck.Result(io.student, io.pages, io.previous_total, (), io.source) if r is io else r
                   for r in bad_results]
    bad_after = after.copy()
    bad_after.students["S0003"] = ck.replace(bad_after.students["S0003"], total=11)
    got = audit.checks(month.records(), p, start, with_new, bad_after, bad_results, TABLE)
    failed = [t for ok, t in got if not ok]
    assert any("previous total + this month" in t and "Ada Example" in t for t in failed)
    assert any("carried forward unchanged" in t and "Absent Al" in t for t in failed)
    assert any("prizes are exactly" in t and "Io Li" in t for t in failed)
    assert any("growth of all totals" in t for t in failed)
    assert audit.report(got, [])[0].startswith("Checks: 4 FAILED")


def test_finishing_writes_the_sheets_tab_and_the_checks(month):
    text = month.finish()
    assert "Checks: all passed" in text and "Spot-check against the scans" in text
    assert "pages changed, added or replaced in review" in text
    wb = openpyxl.load_workbook(month.folder.checkpoint)
    rows = list(wb["Sheets"].iter_rows(values_only=True))
    assert list(rows[0]) == audit.SHEET_COLS and len(rows) == 7
    ck.load(month.folder.checkpoint)                       # the extra tab does not upset loading


def test_spot_checks_are_the_same_every_time_for_a_month(month):
    *_, results = parts(month)
    rows = audit.sheet_rows(month.plan(), DECISIONS, results)
    a = [r.page for r in audit.spot_checks(rows, "2026-10")[0][1]]
    assert a == [r.page for r in audit.spot_checks(rows, "2026-10")[0][1]] and len(a) == 5        # any sheet that counted toward a child
