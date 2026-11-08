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


def test_a_wrong_month_on_the_sheet_is_only_a_warning_and_the_batch_month_is_used(cp):
    records = [rec(1, "Ada Example", "Lopez", 30, month="September 2026"), rec(2, "Io Li", "Park", 5, month="")]
    p = rv.plan(records, cp, "October 2026", {})
    assert p.ready
    assert p.settled[0].warnings == ["sheet says 'September 2026'; counted for October 2026"] and p.settled[1].warnings == []
    assert rv.plan([rec(1, "Ada Example", "Lopez", 30, month="October")], cp, "October 2026", {}).settled[0].warnings == []


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
    ("11/2", True), ("11/1/26", True), ("11/7/2026", True),          # handed in early next month: still October
    ("11/8", False), ("11/2/25", False), ("12/1", False),
])
def test_what_a_child_writes_for_the_month_is_compared_by_month_and_year(written, ok):
    assert rv.sheet_month_matches(written, "October 2026") is ok


def test_a_note_alone_does_not_block_a_confident_page_count(cp):
    records = [rec(1, "Ada Example", "Lopez", 158, note="rows add up to 140")]
    p = rv.plan(records, cp, "October 2026", {})
    assert p.ready and p.settled[0].pages == 158


def test_remembering_a_written_name_makes_next_month_exact(cp):
    records = [rec(1, "Ioli", "Park", 5)]
    p = rv.plan(records, cp, "October 2026", {"oct.pdf#1": {"student": "S0003", "remember_name": True}})
    assert p.ready
    cp2, _, notes = rv.finalize(p, cp, TABLE)
    assert cp2.students["S0003"].nicknames == "Ioli" and any("also be recognised as 'Ioli'" in n for n in notes)
    assert "Ioli" not in cp.students["S0003"].nicknames                    # the input is untouched
    assert rv.plan(records, cp2, "October 2026", {}).ready                   # exact now, no decision needed
    same = rv.plan([rec(1, "Io Li", "Park", 5)], cp, "October 2026",
                   {"oct.pdf#1": {"student": "S0003", "remember_name": True}})
    assert rv.finalize(same, cp, TABLE)[0].students["S0003"].nicknames == ""   # the real name is not a nickname


def test_a_teacher_decision_records_a_class_change(cp):
    p = rv.plan([rec(1, "Ada Example", "Lopez", 30)], cp, "October 2026", {"oct.pdf#1": {"student": "S0001", "teacher": "Park"}})
    cp2, _, notes = rv.finalize(p, cp, TABLE)
    assert cp2.students["S0001"].teacher == "Park" and any("changed from Lopez to Park" in n for n in notes)
    same = rv.plan([rec(1, "Ada Example", "Lopez", 30)], cp, "October 2026", {"oct.pdf#1": {"student": "S0001", "teacher": "Lopez"}})
    assert not any("teacher" in n for n in rv.finalize(same, cp, TABLE)[2])


def sug(records, cp, decisions=None, hints=None):
    decisions = decisions or {}
    p = rv.plan(records, cp, "October 2026", decisions, hints)
    return rv.suggest(p, cp, decisions)


def test_suggestion_for_a_likely_match_settles_it_when_accepted(cp):
    records = [rec(1, "Sam Sampel", "Park", 12)]
    s = sug(records, cp)["oct.pdf#1"]
    assert s.decision == {"student": "S0002", "remember_name": True, "pages": 12}
    assert rv.plan(records, cp, "October 2026", {"oct.pdf#1": s.decision}).ready


def test_suggestion_for_an_unknown_child_is_a_new_student_with_the_guessed_teacher(cp):
    s = sug([rec(1, "Quentin Quimby", "Ms. Park", 40)], cp)["oct.pdf#1"]
    assert s.decision == {"new_student": {"name": "Quentin Quimby", "teacher": "Park", "grade": ""}, "pages": 40}
    assert "not on the roster" in s.reasons[0]


def test_no_suggestion_when_the_teacher_or_pages_cannot_be_guessed(cp):
    s = sug([rec(1, "Quentin Quimby", "Mr. Nobody", 40)], cp)["oct.pdf#1"]
    assert s.decision is None and s.draft["new_student"]["teacher"] == "" and any("teacher" in r for r in s.reasons)
    s = sug([rec(1, "Sam Sampel", "Park", None)], cp)["oct.pdf#1"]
    assert s.decision is None and any("pages" in r for r in s.reasons)


def test_suggestion_confirms_an_unsure_page_count(cp):
    s = sug([rec(1, "Ada Example", "Lopez", 158, pages_confidence=rv.LOW, note="two totals: 158 vs 2,300")], cp)["oct.pdf#1"]
    assert s.decision["pages"] == 158 and any("two totals" in r for r in s.reasons)


def test_suggestion_fills_a_missing_teacher_from_the_sheet(cp):
    cp.students["S0002"] = ck.replace(cp.students["S0002"], teacher="")
    s = sug([rec(1, "Sam Sample", "Ms. Lopez", 5)], cp)["oct.pdf#1"]
    assert s.decision["teacher"] == "Lopez" and any("no teacher on file" in r for r in s.reasons)


def test_suggestion_for_a_second_sheet(cp):
    same = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Ada Example", "Lopez", 30)]
    assert sug(same, cp)["oct.pdf#2"].decision["duplicate"] == "skip"
    more = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Ada Example", "Lopez", 20)]
    assert sug(more, cp)["oct.pdf#2"].decision["duplicate"] == "sum"


def test_suggestion_uses_last_years_name_and_grade(cp):
    last_year = ck.Checkpoint()
    ck.add_student(last_year, "Quentin Quimby", "Old Teacher", "1")
    s = sug([rec(1, "Quentin", "Park", 5)], cp, hints=last_year)["oct.pdf#1"]
    assert s.decision["new_student"] == {"name": "Quentin Quimby", "teacher": "Park", "grade": "1"}


def test_a_confident_match_on_a_hard_to_read_name_says_so(cp):
    s = sug([rec(1, "Ada Example", "Lopez", 30, confidence=rv.LOW)], cp)["oct.pdf#1"]
    assert s.decision["student"] == "S0001" and s.reasons[0].startswith("name hard to read")
