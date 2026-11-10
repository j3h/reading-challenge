"""Starting checkpoint from the master workbook. A made-up workbook with made-up children."""
import sys
from pathlib import Path

import openpyxl
import pytest
from openpyxl.styles import Font

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rc import checkpoint as ck  # noqa: E402
from rc import import_workbook as iw  # noqa: E402

HDR = ["Full Name", "Teacher", "25/26 Grade", "Previous Total", "Current Month Total",
       "Grand Total Through Current Month", "Prize Level Reached", "Prize"]
STOCK = [["Prize", "Level", "Count", "in stock"], ["pen", 2500, 3, 1]]          # the prize-stock table under each tab


def tab(wb, title, rows, header=HDR):
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in rows + STOCK:
        ws.append(r)
    return ws


@pytest.fixture
def workbook(tmp_path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    tab(wb, "May 2026", [["Ada Example", "Lopez", 2, 50, 24, 74], ["Io Li", "Park", 3, 2000, 400, 2400]])
    tab(wb, "Summer 2026", [["Io Li", "Park", 3, 2400, 100, 2500]])
    primary = tab(wb, "PRIMARY", [
        ["Ada Example", "Lopez", 2.0, 74],              # clean: last tab May, 74
        ["Staff Reader", "Teacher", None, 40],          # a staff member reads too: "Teacher" is their class
        ["Io Li", "Park", 3.0, 2500 + 300 + 200],       # PRIMARY already includes October (+200)
        ["Sam Sample", "Park", "K", 100],               # September pages never copied in (PRIMARY 100, Sept 100+50)
        ["Old Timer", "Lopez", 5.0, 999],               # on no month tab: PRIMARY used
        ["Typo Kid", "Lopez", 1.0, 520],                # disagrees with no explanation
    ], header=[None] + HDR[1:])
    primary["A7"].font = Font(color="FFFF0000")         # Typo Kid in red
    tab(wb, "September 2026", [["Io Li", "Park", 3, 2500, 100, 2600], ["Io Li", "Park", 3, 2600, 200, 2800],
                               ["Sam Sample", "Park", "K", 100, 50, 150], ["Typo Kid", "Lopez", 1, 0, 502, 502],
                               ["Gone Kid", "Lopez", 4, 10, 10, 20]])
    tab(wb, "October 2026", [["Io Li", "Park", 3, 2800, 200, 3000], ["Sam Sample", "Park", "K", 100, 50, 150]])
    wb.create_sheet("PRIZE LIST")
    path = tmp_path / "master.xlsx"
    wb.save(path)
    return path


def by_name(cp):
    return {s.name: s for s in cp.students.values()}


def test_tab_titles_become_months():
    assert [iw.tab_month(t) for t in ("Oct 2021", "FEB-2 2022", "Sept 2023", "Summer 2026", "September 2026",
                                      "PRIMARY", "PRIZE LIST", "archived 2024", "Sheet1")] == \
        ["2021-10", "2022-02", "2023-09", "2026-07", "2026-09", None, None, None, None]


def test_totals_are_the_end_of_the_chosen_month(workbook):
    cp, report = iw.parse_workbook(workbook, "September 2026")
    s = by_name(cp)
    assert cp.month == "2026-09" and set(s) == {"Ada Example", "Staff Reader", "Io Li", "Sam Sample", "Old Timer",
                                                "Typo Kid"}
    assert s["Staff Reader"].teacher == "Teacher" and s["Staff Reader"].total == 40
    assert s["Ada Example"].total == 74 and s["Ada Example"].notes == ""
    assert s["Io Li"].total == 2800                                    # two chained sheets in September
    assert "already includes October 2026 (+200)" in s["Io Li"].notes
    assert s["Sam Sample"].total == 150 and "PRIMARY said 100" in s["Sam Sample"].notes
    assert s["Old Timer"].total == 999                                 # only PRIMARY knows them
    assert s["Typo Kid"].total == 502 and "check" in s["Typo Kid"].notes and "red in PRIMARY" in s["Typo Kid"].notes
    assert s["Sam Sample"].grade == "K" and s["Ada Example"].grade == "2"
    assert any("1 children on month tabs are not on PRIMARY" in r for r in report)
    assert report[0].startswith("Totals as of the end of September 2026, from 3 month tabs")


def test_history_is_kept_month_by_month(workbook):
    cp, _ = iw.parse_workbook(workbook, "September 2026")
    io = by_name(cp)["Io Li"].id
    assert [(h.month, h.pages, h.total_after) for h in cp.history if h.student_id == io] == \
        [("2026-05", 400, 2400), ("2026-07", 100, 2500), ("2026-09", 300, 2800)]


def test_sheets_that_do_not_chain_are_flagged(workbook):
    cp, report = iw.parse_workbook(workbook, "October 2026")
    assert by_name(cp)["Io Li"].total == 3000 and "do not chain" not in by_name(cp)["Io Li"].notes
    rows = [iw.MonthRow("X", "Barry", 3870, 2798, 6668), iw.MonthRow("X", "Barry", 3870, 1173, 5043)]
    end, pages, problem = iw.month_end(rows)
    assert end == 6668 and "do not chain" in problem


def test_ids_are_kept_from_an_earlier_checkpoint(workbook):
    old = ck.Checkpoint()
    for sid, name in (("S0007", "Io Li"), ("S0003", "Sam Sample"), ("S0020", "Someone Else")):
        old.students[sid] = ck.Student(sid, name, "Park")
    cp, _ = iw.parse_workbook(workbook, "September 2026", keep_ids=old)
    s = by_name(cp)
    assert s["Io Li"].id == "S0007" and s["Sam Sample"].id == "S0003"
    assert s["Ada Example"].id == "S0021" and len({x.id for x in cp.students.values()}) == 6   # new ids after all old ones


def test_a_missing_month_tab_is_a_clear_error(workbook):
    with pytest.raises(ck.CheckpointError, match="no tab for August 2026"):
        iw.parse_workbook(workbook, "August 2026")


def test_the_saved_checkpoint_loads_and_takes_october(workbook, tmp_path):
    cp, _ = iw.parse_workbook(workbook, "September 2026", {"Park": "Parks"})
    assert by_name(cp)["Io Li"].teacher == "Parks"
    ck.save(cp, tmp_path / "c.xlsx")
    back = ck.load(tmp_path / "c.xlsx")
    assert back.month == "2026-09" and len(back.history) == len(cp.history)
    import make_certificates as mc
    new, results = ck.apply_month(back, "2026-10", [ck.Entry(by_name(back)["Io Li"].id, 200, "oct p. 1")],
                                  mc.load_levels())
    assert results[0].previous_total == 2800 and results[0].student.total == 3000
