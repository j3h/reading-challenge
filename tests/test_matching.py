"""Matching sheet names to students. Fake children only."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rc import checkpoint as ck  # noqa: E402
from rc import matching as mt  # noqa: E402


def roster(*rows):
    cp = ck.Checkpoint()
    for name, teacher in rows:
        ck.add_student(cp, name, teacher)
    return cp


CP = roster(("Ada Example", "Lopez"), ("Amelia Anderson", "Knutsen"), ("Ida Anderson", "Barry"),
            ("Leonard Anderson", "Hjorth"), ("Crosby A.", "Biello/Tanner"), ("Jonathan Smith", "Park"),
            ("Zoe Zimmer", "Park"))


def test_exact_ignores_case_punctuation_and_accents():
    for written in ("ada example", "ADA  EXAMPLE", "Ada Example.", "Ada Exámple"):
        m = mt.match(written, "", CP)
        assert m.status == mt.EXACT and m.best.student.name == "Ada Example"


def test_same_words_in_other_order_is_exact():
    assert mt.match("Example Ada", "Lopez", CP).status == mt.EXACT


def test_misspelling_is_likely_not_exact():
    m = mt.match("Jonathon Smith", "Park", CP)
    assert m.status == mt.LIKELY and m.best.student.name == "Jonathan Smith"


def test_first_name_only_is_likely_at_best_and_scores_higher_with_the_teacher():
    with_teacher, without = mt.match("Zoe", "Park", CP), mt.match("Zoe", "", CP)
    assert with_teacher.status == mt.LIKELY and without.status == mt.LIKELY     # a person confirms either way
    assert with_teacher.best.score > without.best.score and with_teacher.best.reason.startswith("first name only")


def test_two_andersons_are_ambiguous_until_the_teacher_decides():
    no_teacher = mt.match("Anderson", "", CP)
    assert no_teacher.status in (mt.AMBIGUOUS, mt.NONE)
    first_only = mt.match("Ida", "Barry", CP)
    assert first_only.best.student.name == "Ida Anderson" and "same teacher" in first_only.best.reason


def test_teacher_conflict_lowers_confidence_but_does_not_forbid():
    m = mt.match("Jonathan Smith", "Lopez", CP)
    assert m.best.student.name == "Jonathan Smith" and m.status != mt.EXACT


def test_last_initial_in_the_roster():
    m = mt.match("Crosby", "Biello/Tanner", CP)
    assert m.best.student.name == "Crosby A." and m.status in (mt.LIKELY, mt.AMBIGUOUS)
    assert mt.match("Crosby Abbott", "Biello", CP).best.student.name == "Crosby A."


def test_last_initial_on_the_sheet():
    assert mt.match("Ida A.", "Barry", CP).best.student.name == "Ida Anderson"
    other = mt.match("Ida B.", "", CP)
    assert other.status in (mt.NONE, mt.AMBIGUOUS) and (not other.best or other.best.score < 0.8)


def test_teacher_written_differently():
    assert mt.teacher_agreement("Biello", "Biello/Tanner") == "agree"
    assert mt.teacher_agreement("Ms. Tanner", "Biello/Tanner") in ("agree", "unknown")
    assert mt.teacher_agreement("Knutsen", "Knuttsen") == "agree"
    assert mt.teacher_agreement("Lopez", "Park") == "conflict"
    assert mt.teacher_agreement("", "Park") == "unknown"


def test_unknown_name_is_none():
    m = mt.match("Quentin Quimby", "Park", CP)
    assert m.status == mt.NONE and m.candidates == ()


def test_students_who_left_are_not_offered():
    cp = roster(("Ada Example", "Lopez"))
    cp.students["S0001"] = ck.replace(cp.students["S0001"], status=ck.LEFT)
    assert mt.match("Ada Example", "Lopez", cp).status == mt.NONE


def test_last_years_roster_is_offered_as_a_hint():
    last_year = roster(("Quentin Quimby", "Moren"))
    m = mt.match("Quentin Quimby", "Park", CP, hints=last_year)
    assert m.status == mt.HINT and m.best.from_hints and m.best.student.teacher == "Moren"


def test_current_roster_beats_the_hints():
    last_year = roster(("Ada Example", "Old Teacher"))
    m = mt.match("Ada Example", "Lopez", CP, hints=last_year)
    assert m.status == mt.EXACT and not m.best.from_hints


def test_candidates_are_sorted_best_first():
    m = mt.match("Anderson", "", roster(("Amelia Anderson", "A"), ("Ida Anderson", "B"), ("Anderson", "C")))
    scores = [c.score for c in m.candidates]
    assert scores == sorted(scores, reverse=True)


def test_a_full_name_match_beats_a_similar_name_at_the_same_teacher():
    cp = roster(("Ida Anderson", "Barry"), ("Mia Anderson", "Barry"))
    for name in ("Ida Anderson", "Mia Anderson"):
        m = mt.match(name, "Barry", cp)
        assert m.status == mt.EXACT and m.best.student.name == name


def test_identical_names_need_the_teacher_to_tell_them_apart():
    cp = roster(("Sam Sample", "Lopez"), ("Sam Sample", "Park"))
    assert mt.match("Sam Sample", "Park", cp).status == mt.LIKELY
    assert mt.match("Sam Sample", "Park", cp).best.student.teacher == "Park"
    assert mt.match("Sam Sample", "", cp).status == mt.AMBIGUOUS


def test_every_student_in_a_realistic_roster_matches_themselves():
    names = ["Ida Anderson", "Mia Anderson", "Amelia Anderson", "Leonard Anderson", "Zoe Zimmer", "Zoey Zimmer",
             "Jon Smith", "John Smith", "Jonathan Smith"]
    cp = roster(*[(n, "Park") for n in names])
    assert all(mt.match(n, "Park", cp).status == mt.EXACT and mt.match(n, "Park", cp).best.student.name == n
               for n in names)


def test_honorifics_on_the_teacher_are_ignored():
    for written in ("Ms. Biello", "Mrs Biello", "Mr. Biello", "Miss Biello", "biello"):
        assert mt.teacher_agreement(written, "Biello/Tanner") == "agree"
    assert mt.teacher_agreement("Ms. Valters", "Walters") == "agree"
    assert mt.teacher_agreement("Ms", "Walters") == "unknown"
