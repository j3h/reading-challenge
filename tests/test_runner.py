"""A month, start to finish, through the command line. Fake children only."""
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import __main__ as cli  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402

TABLE = mc.load_levels()


def run(*argv):
    cli.main(list(argv))


@pytest.fixture
def data(tmp_path):
    """A data folder with a starting checkpoint for 2026-27."""
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)       # S0001
    ck.add_student(cp, "Sam Sample", "Park", "K")                   # S0002
    ck.add_student(cp, "Io Li", "Park", "3", total=2400)            # S0003
    ck.add_student(cp, "Absent Al", "Lopez", "1", total=10)         # S0004: no sheet in October
    start = tmp_path / "2026-27" / "start"
    start.mkdir(parents=True)
    ck.save(cp, start / "checkpoint.xlsx")
    return tmp_path


def sheets(data, records, month="October 2026"):
    mf = wsp.Workspace(data).month_folder(month).create()
    rv.save_records(records, mf.work / "records.json")
    return mf


def rec(page, name, teacher, pages, **kw):
    return rv.SheetRecord("oct.pdf", page, name, teacher, "October 2026", pages, **kw)


CLEAN = [rec(1, "Ada Example", "Lopez", 30), rec(2, "Sam Sample", "Park", 5), rec(3, "Io Li", "Park", 150)]


def test_start_creates_folders_and_says_where_scans_go(data, capsys):
    run("--data", str(data), "start", "--month", "October 2026")
    out = capsys.readouterr().out
    assert "starting checkpoint in 2026-27/start" in out and "4 students" in out
    assert str(data / "2026-27" / "2026-10" / "scans") in out


def test_a_clean_month_finishes_and_writes_everything(data, capsys):
    mf = sheets(data, CLEAN)
    run("--data", str(data), "finish", "--month", "October 2026")
    assert mf.certificates.exists() and mf.summary.exists() and mf.checkpoint.exists()

    new = ck.load(mf.checkpoint)
    assert new.month == "2026-10"
    assert {s.name: s.total for s in new.students.values()} == {
        "Ada Example": 104, "Sam Sample": 5, "Io Li": 2550, "Absent Al": 10}

    certs = pymupdf.open(mf.certificates)
    texts = [p.get_text().replace("\xa0", " ") for p in certs]
    # 2 per letter sheet, each class on its own sheets: Lopez (1 child) then Park (2 children) = 2 sheets
    assert len(texts) == 2 and certs[0].rect.width == 612 and certs[0].rect.height == 792
    assert "Ada Example" in texts[0] and "Lopez  ·  1 of 1" in texts[0]
    assert "Io Li" in texts[1] and "Sam Sample" in texts[1] and "Park  ·  2 of 2" in texts[1]
    lists = pymupdf.open(mf.class_lists)
    assert lists.page_count == 1
    lt = lists[0].get_text().replace("\xa0", " ")
    assert "Prizes to pull from stock" in lt and lt.index("Lopez") < lt.index("Park") and "Io Li" in lt

    summary = mf.summary.read_text()
    assert "Certificates: 3" in summary and "Absent Al (Lopez)" in summary
    assert "1 x Pencil and Stickers" in summary and "1 x Pen" in summary


def test_check_lists_open_questions_and_exits_nonzero(data, capsys):
    sheets(data, [rec(1, "Ada Example", "Lopez", 30), rec(2, "Quentin Quimby", "", 40, confidence=rv.LOW)])
    with pytest.raises(SystemExit) as e:
        run("--data", str(data), "check", "--month", "October 2026")
    out = capsys.readouterr().out
    assert e.value.code == 1 and "1 open question" in out and "Quentin Quimby" in out and "[who]" in out


def test_check_exits_zero_when_everything_is_settled(data, capsys):
    sheets(data, CLEAN)
    with pytest.raises(SystemExit) as e:
        run("--data", str(data), "check", "--month", "October 2026")
    assert e.value.code == 0 and "0 open question" in capsys.readouterr().out


def test_finish_refuses_while_questions_are_open_and_writes_nothing(data):
    mf = sheets(data, [rec(1, "Quentin Quimby", "", 40)])
    with pytest.raises(SystemExit, match="still open"):
        run("--data", str(data), "finish", "--month", "October 2026")
    assert not mf.certificates.exists() and not mf.summary.exists() and not mf.checkpoint.exists()


def test_deciding_from_the_command_line_then_finishing(data):
    mf = sheets(data, [rec(1, "Ada Example", "Lopez", 30), rec(2, "Quentin Quimby", "", 150)])
    month = ("--month", "October 2026")
    run("--data", str(data), "decide", *month, "oct.pdf#2", "--new-name", "Quentin Quimby")   # teacher missing
    with pytest.raises(SystemExit, match="still open"):
        run("--data", str(data), "finish", *month)
    run("--data", str(data), "decide", *month, "oct.pdf#2", "--teacher", "Moren", "--grade", "1")
    run("--data", str(data), "finish", *month)
    new = ck.load(mf.checkpoint)
    quentin = next(s for s in new.students.values() if s.name == "Quentin Quimby")
    assert (quentin.teacher, quentin.grade, quentin.total) == ("Moren", "1", 150)
    assert "Moren" in "".join(p.get_text() for p in pymupdf.open(mf.certificates))


def test_decide_rejects_an_unknown_sheet_and_an_empty_decision(data):
    sheets(data, CLEAN)
    with pytest.raises(SystemExit, match="no sheet"):
        run("--data", str(data), "decide", "--month", "October 2026", "oct.pdf#99", "--skip", "x")
    with pytest.raises(SystemExit, match="Nothing to record"):
        run("--data", str(data), "decide", "--month", "October 2026", "oct.pdf#1")


def test_a_finished_month_cannot_be_run_again(data):
    sheets(data, CLEAN)
    run("--data", str(data), "finish", "--month", "October 2026")
    with pytest.raises(SystemExit, match="already finished"):
        run("--data", str(data), "finish", "--month", "October 2026")


def test_next_month_starts_from_this_months_checkpoint(data):
    sheets(data, CLEAN)
    run("--data", str(data), "finish", "--month", "October 2026")
    mf = sheets(data, [rv.SheetRecord("nov.pdf", 1, "Ada Example", "Lopez", "November 2026", 20)], "November 2026")
    run("--data", str(data), "finish", "--month", "November 2026")
    new = ck.load(mf.checkpoint)
    assert next(s for s in new.students.values() if s.name == "Ada Example").total == 124
    assert len(new.history) == 4          # three from October, one from November


def test_totals_roll_forward_into_the_next_school_year(data):
    sheets(data, CLEAN)
    run("--data", str(data), "finish", "--month", "October 2026")
    # September 2027 is in the next school-year folder, but starts from October 2026's checkpoint
    mf = sheets(data, [rv.SheetRecord("sep.pdf", 1, "Ada Example", "Park", "September 2027", 12)], "September 2027")
    assert mf.path == data / "2027-28" / "2027-09"
    m = runner.Month(wsp.Workspace(data), "September 2027")
    prev, why = m.previous()
    assert len(prev.students) == 4 and "October 2026" in why
    p = m.plan()          # new teacher this year: a person confirms the class change
    assert [i.kind for i in p.issues] == ["who"] and p.issues[0].candidates[0].student.name == "Ada Example"
    run("--data", str(data), "decide", "--month", "September 2027", "sep.pdf#1", "--student", "S0001",
        "--teacher", "Park")
    run("--data", str(data), "finish", "--month", "September 2027")
    ada = ck.load(m.folder.checkpoint).students["S0001"]
    assert (ada.total, ada.teacher) == (74 + 30 + 12, "Park")


def test_nothing_to_do_when_every_sheet_is_skipped(data):
    sheets(data, [rec(1, "Scribble", "", None)])
    run("--data", str(data), "decide", "--month", "October 2026", "oct.pdf#1", "--skip", "blank")
    with pytest.raises(SystemExit, match="nothing to make"):
        run("--data", str(data), "finish", "--month", "October 2026")


def test_missing_data_folder_and_missing_records_are_clear_errors(tmp_path):
    with pytest.raises(SystemExit, match="Say where the data folder is"):
        run("start", "--month", "October 2026")
    wsp.Workspace(tmp_path)
    with pytest.raises(SystemExit, match="no sheets have been read yet"):
        run("--data", str(tmp_path), "check", "--month", "October 2026")


def test_import_master_command_refuses_to_overwrite(data, tmp_path):
    existing = data / "2026-27" / "start" / "checkpoint.xlsx"
    with pytest.raises(SystemExit, match="already exists"):
        run("--data", str(data), "import-master", str(tmp_path / "m.pdf"), "--season", "2026-27")
    assert existing.exists()


def test_an_existing_student_with_no_teacher_is_resolved_from_the_command_line(data):
    start = data / "2026-27" / "start" / "checkpoint.xlsx"
    cp = ck.load(start)
    cp.students["S0002"] = ck.replace(cp.students["S0002"], teacher="")
    ck.save(cp, start)
    mf = sheets(data, [rec(1, "Sam Sample", "", 150)])
    month = ("--month", "October 2026")
    with pytest.raises(SystemExit, match="1"):
        run("--data", str(data), "check", *month)
    run("--data", str(data), "decide", *month, "oct.pdf#1", "--teacher", "Park")      # bare --teacher: for Sam
    run("--data", str(data), "finish", *month)
    sam = next(s for s in ck.load(mf.checkpoint).students.values() if s.name == "Sam Sample")
    assert (sam.teacher, sam.total) == ("Park", 150)
    assert "teacher for Sam Sample set to Park" in mf.summary.read_text()
