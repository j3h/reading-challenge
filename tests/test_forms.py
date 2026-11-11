"""Google Form responses (CSV) as review sheets. Made-up children and submissions."""
import csv
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import app as app_mod  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import events as ev  # noqa: E402
from rc import forms  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402

HEADER = ["Timestamp", "Student Name (first and last)", "Grade and Teacher", "Month ",
          "How many pages did you read during this month?"]
ROWS = [["10/7/2026 19:34:35", "Ada Example", "K- Lopez", "September", "893"],
        ["11/2/2026 16:11:36", "Io Li", "3rd- Park", "October", "1,579"],
        ["11/3/2026 18:37:20", "Zed Newcomer", "1st- Moren", "October", "868"],
        ["11/4/2026 08:00:00", "Ada Example", "K- Lopez", "", "50"],
        ["11/4/2026 09:00:00", "Sam Sample", "2nd- Park", "Oct", "lots"]]


def write_csv(path, rows=ROWS, header=HEADER):
    buf = io.StringIO()
    csv.writer(buf).writerows([header] + rows)
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


def test_grade_and_teacher_are_split():
    assert forms.split_grade_teacher("K- Hjorth") == ("K", "Hjorth")
    assert forms.split_grade_teacher("3rd- Harris") == ("3", "Harris")
    assert forms.split_grade_teacher("2nd grade - Ms. Barry") == ("2", "Ms. Barry")
    assert forms.split_grade_teacher("Ms. Barry") == ("", "Ms. Barry")


def test_only_this_months_rows_become_sheets(tmp_path):
    recs, left_out = forms.records(write_csv(tmp_path / "responses.csv"), "October 2026")
    assert [(r.page, r.name, r.teacher, r.grade, r.pages) for r in recs] == \
        [(3, "Io Li", "Park", "3", 1579), (4, "Zed Newcomer", "Moren", "1", 868), (6, "Sam Sample", "Park", "2", None)]
    assert recs[0].submitted == "11/2/2026 16:11:36" and "Google Form" in recs[0].note and recs[0].is_form
    assert recs[2].pages_confidence == rv.UNREADABLE and "'lots'" in recs[2].note
    assert left_out == ["responses.csv row 5 (Ada Example): no month given; left out"]


def test_a_csv_without_the_needed_columns_is_refused(tmp_path):
    with pytest.raises(ck.CheckpointError, match="no column for pages"):
        forms.records(write_csv(tmp_path / "x.csv", [["a", "b"]], ["Timestamp", "Name"]), "October 2026")


@pytest.fixture
def data(tmp_path):
    log = ev.EventLog(tmp_path, "mac")
    src = ({"workbook": "m.xlsx", "tab": "Sept", "row": 2},)
    for sid, name, teacher, total in (("S0001", "Io Li", "Park", 2400), ("S0002", "Sam Sample", "Park", 10)):
        log.append([ev.Event("student_added", {"student": sid, "name": name, "teacher": teacher, "grade": "3"}, "Josh",
                             "", src, "2026-09-28T12:00:00+00:00"),
                    ev.Event("total_set", {"student": sid, "total": total}, "Josh", "opening", src,
                             "2026-09-28T12:00:01+00:00")])
    return tmp_path


def test_form_rows_join_the_months_review_and_are_recorded_with_their_row(data):
    m = runner.Month(wsp.Workspace(data), "October 2026")
    m.folder.create()
    write_csv(m.folder.scans / "responses.csv")
    rv.save_records([rv.SheetRecord("oct.pdf", 1, "Io Li", "Park", "October", 1579)], m.records_path)  # paper too
    p = m.plan()
    kinds = {i.key: i.kind for i in p.issues}
    assert kinds["responses.csv#3"] == "duplicate"           # the same child on paper and on the form
    assert kinds["responses.csv#4"] == "who" and kinds["responses.csv#6"] == "pages"
    cp, _ = m.previous()
    sug = rv.suggest(p, cp, {})
    assert sug["responses.csv#3"].decision["duplicate"] == "skip"           # same pages: probably the same reading
    assert sug["responses.csv#4"].decision is None          # Moren is not a teacher on the roster yet
    assert sug["responses.csv#4"].draft["new_student"] == {"name": "Zed Newcomer", "teacher": "", "grade": "1"}
    rv.save_decisions({"responses.csv#3": {"duplicate": "skip"},
                       "responses.csv#4": {"new_student": {"name": "Zed Newcomer", "teacher": "Moren", "grade": "1"}},
                       "responses.csv#6": {"pages": 200}}, m.decisions_path)
    assert "Checks: all passed" in m.finish(who="Jamie")
    st = m.log.state()
    zed = next(s for s in st.students.values() if s.name == "Zed Newcomer")
    rec = next(e for e in st.by_student[zed.id] if e.kind == "pages_recorded")
    assert rec.sources == ({"form": "responses.csv", "row": 4, "submitted": "11/3/2026 18:37:20"},)
    assert st.students[zed.id].grade == "1" and st.students["S0002"].total == 210


def test_the_app_accepts_a_form_csv_and_counts_this_months_rows(data, monkeypatch, tmp_path):
    monkeypatch.setenv("READING_CHALLENGE_SETTINGS", str(tmp_path / "settings.json"))
    home = app_mod.Home(data)
    home.create_month("2026-10")
    body = write_csv(tmp_path / "upload.csv").read_bytes()
    st = home.add_scan("2026-10", "responses.csv", io.BytesIO(body), len(body))
    m = st["months"][0]
    assert m["forms"] == [{"name": "responses.csv", "rows": 3}] and m["step"] == "review" and m["records"]
    bad = b"Timestamp,Name\n1,2\n"
    with pytest.raises(ck.CheckpointError, match="no column for pages"):
        home.add_scan("2026-10", "bad.csv", io.BytesIO(bad), len(bad))
    assert not (data / "2026-27" / "2026-10" / "scans" / "bad.csv").exists()
