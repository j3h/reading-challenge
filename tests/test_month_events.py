"""Months in a data folder with an event log: start from the log, finish into it, reopen, correct. Fake children."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import events as ev  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402

TABLE = mc.load_levels()
WB = {"workbook": "master.xlsx", "tab": "September 2026", "row": 2}


def opening(sid, name, teacher, total, not_given=()):
    """Like the workbook import: added, opening total, and every prize up to it handed out (except `not_given`)."""
    given = [t for t, _, _ in TABLE if t <= total and t not in not_given]
    return [ev.Event("student_added", {"student": sid, "name": name, "teacher": teacher, "grade": "2"}, "Josh", "",
                     (WB,), "2026-09-28T12:00:00+00:00"),
            ev.Event("total_set", {"student": sid, "total": total}, "Josh", "opening balance", (WB,),
                     "2026-09-28T12:00:01+00:00")] + \
        ([ev.Event("prizes_given", {"student": sid, "month": "2026-09", "levels": given}, "Josh", "assumed", (),
                   "2026-09-28T12:00:02+00:00")] if given else [])


@pytest.fixture
def data(tmp_path):
    log = ev.EventLog(tmp_path, "jamies-mac")
    log.append(opening("S0001", "Ada Example", "Lopez", 74) + opening("S0002", "Io Li", "Park", 2400))
    return tmp_path


def month(data, key, records, decisions=None, device="jamies-mac"):
    m = runner.Month(wsp.Workspace(data), key)
    m.log = ev.EventLog(data, device)
    m.folder.create()
    rv.save_records(records, m.records_path)
    if decisions:
        rv.save_decisions(decisions, m.decisions_path)
    return m


def rec(file, page, name, teacher, pages, month="October 2026"):
    return rv.SheetRecord(file, page, name, teacher, month, pages)


def test_a_month_starts_from_the_log_and_finishing_records_it(data):
    m = month(data, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30), rec("photo.jpg", 1, "Io Li", "Park", 150),
                                rec("oct.pdf", 2, "Zed Newcomer", "Moren", 40)],
              {"oct.pdf#2": {"new_student": {"name": "Zed Newcomer", "teacher": "Moren", "grade": "1"}}})
    cp, why = m.previous()
    assert "event log" in why and cp.students["S0002"].total == 2400 and cp.month is None
    with pytest.raises(ck.CheckpointError, match="say who"):
        m.finish()
    text = m.finish(who="Jamie")
    assert "Checks: all passed" in text and "the event log, replayed, gives the same totals" in text
    st = m.log.state()
    assert st.students["S0002"].total == 2550 and 2500 in st.prizes_given["S0002"]
    zed = next(s for s in st.students.values() if s.name == "Zed Newcomer")
    kinds = [(e.kind, e.sources) for e in st.by_student[zed.id]]
    assert kinds[0] == ("student_added", ({"scan": "oct.pdf", "page": 2},)) and kinds[1][0] == "pages_recorded"
    io_rec = next(e for e in st.by_student["S0002"] if e.kind == "pages_recorded")
    assert io_rec.sources == ({"photo": "photo.jpg"},) and io_rec.who == "Jamie" and io_rec.batch
    assert ck.load(m.folder.checkpoint).students["S0002"].total == 2550
    with pytest.raises(ck.CheckpointError, match="already finished"):
        m.previous()


def test_a_correction_after_finishing_carries_into_the_next_month(data):
    oct_ = month(data, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30)])
    oct_.finish(who="Jamie")
    log = ev.EventLog(data, "jamies-mac")
    log.append([ev.Event("pages_adjusted", {"student": "S0001", "month": "2026-10", "delta": 300}, "Jamie",
                         "sheet's total missed a page of books", ({"manual": True},))])
    nov = month(data, "2026-11", [rec("nov.pdf", 1, "Ada Example", "Lopez", 100, "November 2026")])
    cp, _ = nov.previous()
    assert cp.students["S0001"].total == 74 + 30 + 300 and cp.month == "2026-10"
    nov.finish(who="Jamie")
    st = log.state()
    assert st.students["S0001"].total == 504
    assert sorted(st.prizes_given["S0001"]) == [1, 100, 500]   # opening 74 had 1; Oct crossed 100, Nov 500


def test_reopening_cancels_exactly_that_months_events_and_it_can_be_finished_again(data):
    oct_ = month(data, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30)])
    oct_.finish(who="Jamie")
    nov = month(data, "2026-11", [rec("nov.pdf", 1, "Ada Example", "Lopez", 10, "November 2026")])
    nov.finish(who="Jamie")
    with pytest.raises(ck.CheckpointError, match="reopen that one first"):
        runner.reopen_month(oct_, "Jamie")
    assert runner.reopen_month(nov, "Jamie", "wrong scan") == 2          # month_finished + pages_recorded
    assert runner.reopen_month(oct_, "Jamie") == 3                       # ... + prizes_given (crossed 100)
    st = ev.EventLog(data).state()
    assert st.students["S0001"].total == 74 and not st.finished and st.prizes_given["S0001"] == {1}   # opening only
    cancels = [e for e in st.events if e.kind == "event_voided"]
    assert len(cancels) == 5 and all(e.who == "Jamie" for e in cancels)
    assert sum("wrong scan" in e.reason for e in cancels) == 2 and sum(e.reason == "reopened October 2026" for e in cancels) == 3
    for p in (oct_.folder.checkpoint, oct_.folder.certificates, oct_.folder.summary):
        p.unlink()                                                       # as the app's reopen moves them aside
    rv.save_records([rec("oct.pdf", 1, "Ada Example", "Lopez", 35)], oct_.records_path)
    oct_.finish(who="Jamie")
    assert ev.EventLog(data).state().students["S0001"].total == 74 + 35


def test_the_same_month_finished_on_two_macs_counts_once(data):
    a = month(data, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30)])
    cp, _ = a.previous()
    p = a.plan()
    with_new, entries, _ = rv.finalize(p, cp, TABLE)
    _, results = ck.apply_month(with_new, "2026-10", entries, TABLE)
    # both Macs finish October before either has seen the other's events
    events_a = runner.month_events(p, cp, with_new, results, "2026-10", "Jamie")
    events_b = runner.month_events(p, cp, with_new, results, "2026-10", "Josh")
    ev.EventLog(data, "jamies-mac").append(events_a)
    ev.EventLog(data, "joshs-mac").own_file.write_text("".join(
        ev.Event(e.kind, e.data, e.who, e.reason, e.sources, e.time, "joshs-mac", e.id, e.batch).to_json() + "\n"
        for e in events_b))
    st = ev.EventLog(data).state()
    assert st.students["S0001"].total == 104 and any("finished on two Macs" in w for w in st.warnings)


def test_folders_without_a_log_still_work_from_checkpoint_files(tmp_path):
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)
    (tmp_path / "2026-27" / "start").mkdir(parents=True)
    ck.save(cp, tmp_path / "2026-27" / "start" / "checkpoint.xlsx")
    m = month(tmp_path, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30)])
    assert not m.uses_events()
    m.finish()
    assert ck.load(m.folder.checkpoint).students["S0001"].total == 104 and not (tmp_path / "events").exists()


def test_prizes_owed_from_earlier_are_bagged_and_recorded(tmp_path):
    import pymupdf
    data = tmp_path
    log = ev.EventLog(data, "jamies-mac")
    # Io Li reached 2,500 before October, but the Pen was never handed out
    log.append(opening("S0001", "Ada Example", "Lopez", 74) + opening("S0002", "Io Li", "Park", 2600, not_given=(2500,)))
    m = month(data, "2026-10", [rec("oct.pdf", 1, "Ada Example", "Lopez", 30)])
    text = m.finish(who="Jamie")
    assert "owed from earlier months" in text and "Io Li: " in text and "Pen (2,500)" in text
    st = log.state()
    assert not st.owed(TABLE)                                       # everything owed is now handed out
    given = [e for e in st.by_student["S0002"] if e.kind == "prizes_given" and "owed from earlier" in e.reason]
    assert len(given) == 1 and given[0].data["levels"] == [2500] and given[0].batch
    pdf = pymupdf.open(m.folder.certificates)
    cover_text = "".join(pdf[i].get_text() for i in range(pdf.page_count))
    assert "Pen (owed)" in cover_text and "Park" in cover_text       # Io's class gets a cover page with no sheets
    runner.reopen_month(m, "Jamie")                                 # reopening un-gives them
    assert ev.EventLog(data).state().owed(TABLE)


def test_a_reopened_month_with_new_students_can_be_finished_again(data):
    m = month(data, "2026-10", [rec("oct.pdf", 1, "Zed Newcomer", "Moren", 40), rec("oct.pdf", 2, "Io Li", "Park", 10)],
              {"oct.pdf#1": {"new_student": {"name": "Zed Newcomer", "teacher": "Moren", "grade": "1"}}})
    m.finish(who="Jamie")
    runner.reopen_month(m, "Jamie", "fixing something else")
    for p in (m.folder.checkpoint, m.folder.certificates, m.folder.summary):
        p.unlink()
    text = m.finish(who="Jamie")                 # the same new student, from the same sheet, recorded again
    assert "Checks: all passed" in text
    st = ev.EventLog(data).state()
    zeds = [s for s in st.students.values() if s.name == "Zed Newcomer"]
    assert len(zeds) == 1 and zeds[0].total == 40 and not st.warnings
