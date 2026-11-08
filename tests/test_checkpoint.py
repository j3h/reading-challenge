"""Checkpoint model: applying a month, prize accounting, xlsx round trip. Fake children only."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402

TABLE = mc.load_levels()


@pytest.fixture
def cp():
    c = ck.Checkpoint()
    ck.add_student(c, "Ada Example", "Lopez", "2", total=74, table=TABLE)       # S0001
    ck.add_student(c, "Sam Sample", "Park", "K", total=0, table=TABLE)          # S0002
    ck.add_student(c, "Io Li", "Park", "3", total=2400, table=TABLE)            # S0003
    return c


def labels(result):
    return [p[1] for p in result.prizes]


def test_seed_assumes_earlier_prizes_were_given(cp):
    assert cp.students["S0001"].last_level == 1        # 74 pages: First Pages only
    assert cp.students["S0003"].last_level == 1000     # 2,400: up to 1,000
    assert cp.students["S0002"].last_level == 0


def test_apply_month_totals_and_prizes(cp):
    new, results = ck.apply_month(cp, "October 2026", [ck.Entry("S0001", 30, "p. 1"),
                                                       ck.Entry("S0003", 150, "p. 2")], TABLE)
    by = {r.student.id: r for r in results}
    assert by["S0001"].student.total == 104 and labels(by["S0001"]) == ["100 Pages"]
    assert by["S0003"].student.total == 2550 and labels(by["S0003"]) == ["2,500 Pages"]
    assert new.month == "2026-10"
    assert [(h.student_id, h.pages, h.total_after, h.source) for h in new.history] == \
        [("S0001", 30, 104, "p. 1"), ("S0003", 150, 2550, "p. 2")]


def test_apply_does_not_modify_the_input(cp):
    ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 30)], TABLE)
    assert cp.month is None and cp.students["S0001"].total == 74 and cp.history == []


def test_crossing_several_levels_gives_all_of_them(cp):
    _, results = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 2926)], TABLE)   # 74 -> 3,000
    assert labels(results[0]) == ["100 Pages", "500 Pages", "1,000 Pages", "2,500 Pages"]


def test_no_prize_when_no_level_crossed(cp):
    _, results = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 5)], TABLE)
    assert results[0].prizes == ()


def test_prizes_are_given_only_once(cp):
    cp2, r1 = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 30)], TABLE)
    cp3, r2 = ck.apply_month(cp2, "2026-11", [ck.Entry("S0001", 10)], TABLE)
    assert labels(r1[0]) == ["100 Pages"] and r2[0].prizes == ()
    assert cp3.students["S0001"].last_level == 100


def test_missed_child_keeps_owed_prizes_for_next_time(cp):
    # A child whose last_level lags their total (e.g. total corrected upward by hand) is owed the gap.
    cp.students["S0001"] = ck.replace(cp.students["S0001"], total=600)   # 74 -> 600 edited in Students sheet
    _, results = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 0)], TABLE)
    assert labels(results[0]) == ["100 Pages", "500 Pages"]


def test_children_without_an_entry_are_carried_forward(cp):
    new, results = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 30)], TABLE)
    assert new.students["S0002"] == cp.students["S0002"]
    assert [s.name for s in ck.not_seen(new, results)] == ["Io Li", "Sam Sample"]


def test_month_cannot_be_applied_twice_or_out_of_order(cp):
    new, _ = ck.apply_month(cp, "2026-10", [ck.Entry("S0001", 1)], TABLE)
    for month in ("October 2026", "2026-09"):
        with pytest.raises(ck.CheckpointError, match="already includes October 2026"):
            ck.apply_month(new, month, [], TABLE)
    ck.apply_month(new, "November 2026", [], TABLE)


@pytest.mark.parametrize("entries, message", [
    ([ck.Entry("S9999", 5)], "no such student"),
    ([ck.Entry("S0001", 5), ck.Entry("S0001", 6)], "two entries"),
    ([ck.Entry("S0001", -5)], "negative pages"),
])
def test_bad_entries_stop_the_run(cp, entries, message):
    with pytest.raises(ck.CheckpointError, match=message):
        ck.apply_month(cp, "2026-10", entries, TABLE)


def test_student_who_left_cannot_get_an_entry(cp):
    cp.students["S0002"] = ck.replace(cp.students["S0002"], status=ck.LEFT)
    with pytest.raises(ck.CheckpointError, match="marked left"):
        ck.apply_month(cp, "2026-10", [ck.Entry("S0002", 5)], TABLE)


def test_new_student_needs_a_teacher(cp):
    with pytest.raises(ck.CheckpointError, match="classroom teacher"):
        ck.add_student(cp, "New Kid", "  ")
    s = ck.add_student(cp, "New Kid", "Lopez")
    assert s.id == "S0004" and s.total == 0 and s.last_level == 0


def test_new_student_joining_with_pages_this_month_gets_first_pages(cp):
    s = ck.add_student(cp, "New Kid", "Lopez")
    _, results = ck.apply_month(cp, "2026-10", [ck.Entry(s.id, 40)], TABLE)
    assert labels(results[0]) == ["First Pages"]


def test_validate_catches_missing_teacher_and_duplicates(cp):
    cp.students["S0009"] = ck.Student("S0009", "Ada Example", "Lopez")
    cp.students["S0010"] = ck.Student("S0010", "No Teacher", "")
    problems = ck.validate(cp)
    assert any("duplicate" in p for p in problems) and any("no teacher" in p for p in problems)
    with pytest.raises(ck.CheckpointError, match="no teacher"):
        ck.apply_month(cp, "2026-10", [], TABLE)


def test_classrooms_grouped_by_teacher_then_name(cp):
    _, results = ck.apply_month(cp, "2026-10", [ck.Entry("S0002", 5), ck.Entry("S0003", 5),
                                                ck.Entry("S0001", 5)], TABLE)
    groups = ck.classrooms(results)
    assert [(t, [r.student.name for r in rs]) for t, rs in groups] == \
        [("Lopez", ["Ada Example"]), ("Park", ["Io Li", "Sam Sample"])]


def test_find_by_name_ignores_case_and_spacing(cp):
    assert [s.id for s in ck.find_by_name(cp, "  ada   EXAMPLE ")] == ["S0001"]
    assert ck.find_by_name(cp, "Nobody") == []


def test_xlsx_round_trip(cp, tmp_path):
    new, results = ck.apply_month(cp, "October 2026", [ck.Entry("S0001", 30, "p. 1")], TABLE)
    path = tmp_path / "checkpoint.xlsx"
    ck.save(new, path, results, TABLE)
    back = ck.load(path)
    assert back.month == "2026-10"
    assert back.students == new.students
    assert back.history == new.history


def test_xlsx_fresh_seed_round_trips_with_no_month(cp, tmp_path):
    path = tmp_path / "seed.xlsx"
    ck.save(cp, path)
    back = ck.load(path)
    assert back.month is None and back.students == cp.students


def test_load_accepts_hand_edits_and_text_numbers(cp, tmp_path):
    from openpyxl import load_workbook
    path = tmp_path / "c.xlsx"
    ck.save(cp, path)
    wb = load_workbook(path)
    ws = wb["Students"]
    ws["E2"] = "1,250"           # a person typed a total with a comma
    ws["B3"] = "  Renamed Kid "
    wb.save(path)
    back = ck.load(path)
    assert back.students["S0001"].total == 1250
    assert back.students["S0003"].name == "Renamed Kid"  # row 3 is Io Li: the sheet sorts by teacher, name


def test_load_reports_a_missing_column(cp, tmp_path):
    from openpyxl import load_workbook
    path = tmp_path / "c.xlsx"
    ck.save(cp, path)
    wb = load_workbook(path)
    wb["Students"]["C1"] = "Teachr"
    wb.save(path)
    with pytest.raises(ck.CheckpointError, match="missing column.*Teacher"):
        ck.load(path)
