"""Settling every doubt about the scans before anything is counted. Fake children only."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import review as rv  # noqa: E402

TABLE = mc.load_levels()


@pytest.fixture
def cp():
    c = ck.Checkpoint()
    ck.add_student(c, "Ada Example", "Lopez", "2", total=74)       # S0001
    ck.add_student(c, "Sam Sample", "Park", "K")                   # S0002
    ck.add_student(c, "Io Li", "Park", "3", total=2400)            # S0003
    return c


def rec(page, name, teacher="", pages=10, month="October 2026", **kw):
    return rv.SheetRecord("oct.pdf", page, name, teacher, month, pages, **kw)


def kinds(p):
    return [(i.key, i.kind) for i in p.issues]


def test_clean_exact_sheets_need_no_decisions(cp):
    p = rv.plan([rec(1, "Ada Example", "Lopez", 30), rec(2, "Io Li", "Park", 5)], cp, "October 2026", {})
    assert p.ready
    _, entries, notes = rv.finalize(p, cp, TABLE)
    assert [(e.student_id, e.pages, e.source) for e in entries] == [("S0001", 30, "oct.pdf p. 1"),
                                                                    ("S0003", 5, "oct.pdf p. 2")]
    assert notes == []


def test_a_likely_match_waits_for_a_person(cp):
    records = [rec(1, "Sam Sampel", "Park")]
    p = rv.plan(records, cp, "October 2026", {})
    assert kinds(p) == [("oct.pdf#1", "who")] and p.issues[0].candidates[0].student.id == "S0002"
    p = rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"student": "S0002"}})
    assert p.ready


def test_low_confidence_name_is_not_trusted_even_if_it_matches(cp):
    p = rv.plan([rec(1, "Ada Example", "Lopez", confidence=rv.LOW)], cp, "October 2026", {})
    assert kinds(p) == [("oct.pdf#1", "who")]


def test_unknown_child_must_be_added_with_a_teacher(cp):
    records = [rec(1, "Quentin Quimby", "Moren", 40)]
    assert kinds(rv.plan(records, cp, "October 2026", {})) == [("oct.pdf#1", "who")]
    no_teacher = {"oct.pdf#1": {"new_student": {"name": "Quentin Quimby", "teacher": ""}}}
    p = rv.plan(records, cp, "October 2026", no_teacher)
    assert kinds(p) == [("oct.pdf#1", "teacher")] and "Lopez" in p.issues[0].candidates
    done = {"oct.pdf#1": {"new_student": {"name": "Quentin Quimby", "teacher": "Moren", "grade": "1"}}}
    p = rv.plan(records, cp, "October 2026", done)
    assert p.ready
    cp2, entries, notes = rv.finalize(p, cp, TABLE)
    new = cp2.students[entries[0].student_id]
    assert (new.name, new.teacher, new.grade, new.total) == ("Quentin Quimby", "Moren", "1", 0)
    assert any("new student Quentin Quimby (Moren)" in n for n in notes)
    assert "S0004" not in cp.students                      # the input checkpoint is untouched


def test_new_student_can_join_with_an_existing_total(cp):
    done = {"oct.pdf#1": {"new_student": {"name": "Joiner Jones", "teacher": "Park", "total": 700}}}
    p = rv.plan([rec(1, "Joiner Jones", "Park", 5)], cp, "October 2026", done)
    cp2, entries, _ = rv.finalize(p, cp, TABLE)
    s = cp2.students[entries[0].student_id]
    assert s.total == 700
    _, results = ck.apply_month(cp2, "October 2026", entries, TABLE)
    assert results[0].prizes == ()                     # 700 -> 705 crosses nothing: no prizes for the old total


def test_missing_or_unreadable_pages_need_an_answer(cp):
    records = [rec(1, "Ada Example", "Lopez", None), rec(2, "Io Li", "Park", 99, pages_confidence=rv.UNREADABLE)]
    assert kinds(rv.plan(records, cp, "October 2026", {})) == [("oct.pdf#1", "pages"), ("oct.pdf#2", "pages")]
    p = rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"pages": 12}, "oct.pdf#2": {"pages": 34}})
    assert p.ready and [s.pages for s in p.settled] == [12, 34]


def test_low_confidence_pages_can_be_confirmed_or_overridden(cp):
    records = [rec(1, "Ada Example", "Lopez", 158, pages_confidence=rv.LOW, note="two totals: 158 vs 2300")]
    p = rv.plan(records, cp, "October 2026", {})
    assert kinds(p) == [("oct.pdf#1", "pages")] and "two totals" in p.issues[0].message
    assert rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"pages_ok": True}}).settled[0].pages == 158
    assert rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"pages": 2300}}).settled[0].pages == 2300


def test_wrong_month_on_the_sheet_is_flagged(cp):
    records = [rec(1, "Ada Example", "Lopez", 30, month="September 2026"), rec(2, "Io Li", "Park", 5, month="")]
    assert kinds(rv.plan(records, cp, "October 2026", {})) == [("oct.pdf#1", "month")]
    assert rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"month_ok": True}}).ready
    assert rv.plan([rec(1, "Ada Example", "Lopez", 30, month="9/19")], cp, "October 2026", {}).issues[0].kind == "month"
    assert rv.plan([rec(1, "Ada Example", "Lopez", 30, month="October")], cp, "October 2026", {}).ready


def test_skipping_a_sheet_settles_it_and_is_reported(cp):
    p = rv.plan([rec(1, "Mystery Scribble", "", None)], cp, "October 2026", {"oct.pdf#1": {"skip": "blank sheet"}})
    assert p.ready
    _, entries, notes = rv.finalize(p, cp, TABLE)
    assert entries == [] and "blank sheet" in notes[0]


def test_two_sheets_for_one_child_need_a_decision(cp):
    records = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Ada Example", "Lopez", 20)]
    p = rv.plan(records, cp, "October 2026", {})
    assert kinds(p) == [("oct.pdf#2", "duplicate")]
    summed = rv.plan(records, cp, "October 2026", {"oct.pdf#2": {"duplicate": "sum"}})
    _, entries, _ = rv.finalize(summed, cp, TABLE)
    assert [(e.pages, e.source) for e in entries] == [(50, "oct.pdf p. 1, oct.pdf p. 2")]
    skipped = rv.plan(records, cp, "October 2026", {"oct.pdf#2": {"duplicate": "skip"}})
    assert [e.pages for e in rv.finalize(skipped, cp, TABLE)[1]] == [30]


def test_finalize_refuses_while_anything_is_open(cp):
    p = rv.plan([rec(1, "Nobody Known", "Park", 5)], cp, "October 2026", {})
    with pytest.raises(ck.CheckpointError, match="still open"):
        rv.finalize(p, cp, TABLE)


def test_decision_naming_a_missing_student_is_caught(cp):
    p = rv.plan([rec(1, "Ada Example", "Lopez")], cp, "October 2026", {"oct.pdf#1": {"student": "S9999"}})
    assert kinds(p) == [("oct.pdf#1", "who")]


def test_last_years_roster_is_offered_for_unknown_children(cp):
    last_year = ck.Checkpoint()
    ck.add_student(last_year, "Quentin Quimby", "Old Teacher", "1")
    p = rv.plan([rec(1, "Quentin Quimby", "Park", 5)], cp, "October 2026", {}, hints=last_year)
    assert p.issues[0].candidates[0].from_hints and "last year" in p.issues[0].message


def test_decisions_and_records_survive_a_round_trip(tmp_path):
    decisions = {"oct.pdf#1": {"student": "S0001", "pages": 5}, "oct.pdf#2": {"skip": "blank"}}
    rv.save_decisions(decisions, tmp_path / "work" / "decisions.json")
    assert rv.load_decisions(tmp_path / "work" / "decisions.json") == decisions
    assert rv.load_decisions(tmp_path / "missing.json") == {}
    records = [rec(1, "Ada Example", "Lopez", 30, note="check"), rec(2, "", "", None)]
    rv.save_records(records, tmp_path / "records.json")
    assert rv.load_records(tmp_path / "records.json") == records


def test_damaged_decisions_file_is_a_clear_error(tmp_path):
    (tmp_path / "d.json").write_text("{not json")
    with pytest.raises(ck.CheckpointError, match="damaged"):
        rv.load_decisions(tmp_path / "d.json")


def test_end_to_end_plan_to_printed_totals(cp):
    records = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Quentin Quimby", "Moren", 150)]
    decisions = {"oct.pdf#2": {"new_student": {"name": "Quentin Quimby", "teacher": "Moren"}}}
    p = rv.plan(records, cp, "October 2026", decisions)
    cp2, entries, _ = rv.finalize(p, cp, TABLE)
    new_cp, results = ck.apply_month(cp2, "October 2026", entries, TABLE)
    by = {r.student.name: [x[1] for x in r.prizes] for r in results}
    assert by == {"Ada Example": ["100 Pages"], "Quentin Quimby": ["First Pages", "100 Pages"]}
    assert new_cp.month == "2026-10"


def test_a_student_on_the_roster_with_no_teacher_needs_one_looked_up(cp):
    cp.students["S0002"] = ck.replace(cp.students["S0002"], teacher="")
    records = [rec(1, "Sam Sample", "", 5)]
    p = rv.plan(records, cp, "October 2026", {})
    assert kinds(p) == [("oct.pdf#1", "teacher")] and "no teacher on file" in p.issues[0].message
    assert "Lopez" in p.issues[0].candidates                      # offers the known teachers
    p = rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"teacher": " Park "}})
    assert p.ready
    cp2, entries, notes = rv.finalize(p, cp, TABLE)
    assert cp2.students["S0002"].teacher == "Park" and cp.students["S0002"].teacher == ""
    assert any("teacher for Sam Sample set to Park" in n for n in notes)
    _, results = ck.apply_month(cp2, "October 2026", entries, TABLE)
    assert results[0].student.teacher == "Park"


def test_a_skipped_sheet_does_not_need_a_teacher_looked_up(cp):
    cp.students["S0002"] = ck.replace(cp.students["S0002"], teacher="")
    p = rv.plan([rec(1, "Sam Sample", "", 5)], cp, "October 2026", {"oct.pdf#1": {"skip": "blank"}})
    assert p.ready


@pytest.mark.parametrize("written, ok", [
    ("October", True), ("october", True), ("Oct", True), ("Oct.", True), ("OCT 2026", True), ("October 2026", True),
    ("October, 2026", True), ("10/5", True), ("10/5/26", True), ("10/05/2026", True), ("10-5-2026", True),
    ("2026-10", True),
    ("September", False), ("9/19", False), ("Sept 2026", False), ("October 2025", False), ("10/5/25", False),
    ("Octobre", False), ("", False), ("last month", False), ("Ma", False),
])
def test_what_a_child_writes_for_the_month_is_compared_by_month_and_year(written, ok):
    assert rv.sheet_month_matches(written, "October 2026") is ok
