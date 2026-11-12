"""The inbox and rounds: sheets wait until a round prints the ready ones, for a month chosen then. Fake children."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import events as ev  # noqa: E402
from rc import forms  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402

TABLE = mc.load_levels()
WB = {"workbook": "master.xlsx", "tab": "September 2026", "row": 2}


def opening(sid, name, teacher, total):
    given = [t for t, _, _ in TABLE if t <= total]
    return [ev.Event("student_added", {"student": sid, "name": name, "teacher": teacher, "grade": "2"}, "Josh", "",
                     (WB,), "2026-09-28T12:00:00+00:00"),
            ev.Event("total_set", {"student": sid, "total": total}, "Josh", "opening balance", (WB,),
                     "2026-09-28T12:00:01+00:00")] + \
        ([ev.Event("prizes_given", {"student": sid, "month": "2026-09", "levels": given}, "Josh", "assumed", (),
                   "2026-09-28T12:00:02+00:00")] if given else [])


@pytest.fixture
def data(tmp_path):
    ev.EventLog(tmp_path, "mac").append(opening("S0001", "Ada Example", "Lopez", 74) +
                                        opening("S0002", "Io Li", "Park", 2400) +
                                        opening("S0003", "Sam Sample", "Park", 10))
    return tmp_path


def pool(data, records=(), decisions=None, month="2026-10"):
    p = runner.Pool(wsp.Workspace(data))
    p.log = ev.EventLog(data, "mac")
    p.folder.create()
    existing = rv.load_records(p.records_path) if p.records_path.exists() else []
    rv.save_records(existing + list(records), p.records_path)
    if decisions:
        d = p.decisions()
        d.update(decisions)
        rv.save_decisions(d, p.decisions_path)
    p.set_round_month(month) if month else None
    return p


def rec(file, page, name, teacher, pages, month="October", **kw):
    return rv.SheetRecord(file, page, name, teacher, month, pages, **kw)


def test_default_round_month_is_last_calendar_month():
    import datetime as dt
    assert runner.previous_calendar_month(dt.date(2026, 11, 12)) == "2026-10"
    assert runner.previous_calendar_month(dt.date(2027, 1, 3)) == "2026-12"


def test_a_round_prints_the_ready_sheets_and_leaves_the_rest_waiting(data):
    p = pool(data, [rec("a.pdf", 1, "Ada Example", "Lopez", 30),
                    rec("a.pdf", 2, "Quentin Quimby", "", 40),                       # nobody: needs a decision
                    rec("a.pdf", 3, "Io Li", "Park", 150)])
    c = p.counts()
    assert (c["open"], c["ready"], c["children"]) == (1, 2, 2)
    text = p.finish(who="Jamie")
    assert "October 2026, round 1" in text and "Certificates: 2" in text
    out = wsp.Workspace(data).round_folder("2026-10", 1)
    assert out.certificates.exists() and out.summary.exists() and out.checkpoint.exists()
    st = p.log.state()
    assert st.students["S0001"].total == 104 and st.filed == {"a.pdf#1", "a.pdf#3"}
    assert [r.key for r in p.records()] == ["a.pdf#2"]                               # still waiting
    assert st.rounds[-1].data["round"] == 1


def test_late_sheets_make_a_second_round_with_a_warning_for_a_child_already_printed(data):
    p = pool(data, [rec("a.pdf", 1, "Ada Example", "Lopez", 30)])
    p.finish(who="Jamie")
    p = pool(data, [rec("late.jpg", 1, "Ada Example", "Lopez", 12), rec("late2.jpg", 1, "Sam Sample", "Park", 5)])
    plan = p.plan()                    # Ada already has October pages: a question (hold, skip or accept)
    assert [(i.key, i.kind) for i in plan.issues] == [("late.jpg#1", "repeat")]
    assert "already has 30 pages for October 2026" in plan.issues[0].message
    sug = rv.suggest(plan, p.previous()[0], p.decisions())["late.jpg#1"]
    assert sug.needs_choice and sug.decision["repeat_ok"]
    p.set_decision("late.jpg#1", {"hold": True, "repeat_ok": True})               # Ada's goes into November
    assert [r.key for r in p.held()] == ["late.jpg#1"] and [r.key for r in p.records()] == ["late2.jpg#1"]
    p.finish(who="Jamie")
    assert wsp.Workspace(data).round_folder("2026-10", 2).certificates.exists()
    p.set_round_month("2026-11")
    p.set_decision("late.jpg#1", {"repeat_ok": True})                             # brought back
    p.finish(who="Jamie")
    st = p.log.state()
    assert st.month_pages[("S0001", "2026-11")] == 12 and st.students["S0001"].total == 74 + 30 + 12
    with pytest.raises(ck.CheckpointError, match="already been printed"):
        p.set_round_month("2026-10")


def test_a_child_with_an_open_sheet_waits_whole(data):
    p = pool(data, [rec("a.pdf", 1, "Ada Example", "Lopez", 30),
                    rec("a.pdf", 2, "Ada Example", "Lopez", 20)])                   # second sheet: add/replace/skip?
    with pytest.raises(ck.CheckpointError, match="nothing is ready"):
        p.finish(who="Jamie")
    p.set_decision("a.pdf#2", {"duplicate": "sum"})
    p.finish(who="Jamie")
    assert p.log.state().students["S0001"].total == 124


def test_undo_the_latest_round_puts_its_sheets_back(data):
    p = pool(data, [rec("a.pdf", 1, "Ada Example", "Lopez", 30)])
    p.finish(who="Jamie")
    batch = p.log.state().rounds[-1].batch
    with pytest.raises(ck.CheckpointError, match="say why"):
        p.undo_round(batch, "Jamie", "")
    p.undo_round(batch, "Jamie", "wrong child")
    st = p.log.state()
    assert st.students["S0001"].total == 74 and not st.filed and [r.key for r in p.records()] == ["a.pdf#1"]
    assert not wsp.Workspace(data).round_folder("2026-10", 1).path.exists()
    p.finish(who="Jamie")                                                            # and it can be printed again
    assert wsp.Workspace(data).round_folder("2026-10", 1).certificates.exists()


def test_form_rows_join_the_inbox_by_month_answer(data, tmp_path):
    p = pool(data)
    csv = p.folder.scans / "Responses.csv"
    csv.write_text("Timestamp,Student Name,Grade and Teacher,Month ,How many pages\n"
                   "10/2/2026 9:00:00,Old One,2nd- Lopez,September,10\n"
                   "11/3/2026 18:37:20,Ada Example,2nd- Lopez,October,30\n"
                   "1/4/2027 8:00:00,Io Li,3rd- Park,December,50\n", encoding="utf-8")
    assert forms.resolve_month("December", "1/4/2027 8:00:00") == "2026-12"
    assert forms.resolve_month("October", "11/3/2026 18:37:20") == "2026-10"
    p.save_settings(forms_from="2026-10")
    assert [r.key for r in p.records()] == ["Responses.csv#3", "Responses.csv#4"]


def test_moving_month_folders_into_the_inbox(data):
    m = runner.Month(wsp.Workspace(data), "2026-10")
    m.log = ev.EventLog(data, "mac")
    m.folder.create()
    rv.save_records([rec("oct.pdf", 1, "Ada Example", "Lopez", 30), rec("oct.pdf", 2, "Nobody", "", 5)], m.records_path)
    rv.save_decisions({"oct.pdf#2": {"skip": "blank"}}, m.decisions_path)
    (m.folder.scans / "oct.pdf").write_bytes(b"%PDF fake")
    m.finish(who="Jamie")
    notes = runner.move_to_inbox(wsp.Workspace(data), "Jamie")
    assert "2 sheets filed" in notes[0]
    assert not m.folder.path.exists()
    out = wsp.Workspace(data).round_folder("2026-10", 1)
    assert out.certificates.exists() and out.summary.exists()
    p = runner.Pool(wsp.Workspace(data))
    assert (p.folder.scans / "oct.pdf").exists() and p.settings()["forms_from"] == "2026-10"
    assert p.records() == [] and {r.key for r in p.all_records()} == {"oct.pdf#1", "oct.pdf#2"}
    # undoing round 1 (the old finish) frees its sheets again, skip decision and all
    batch = p.log.state().rounds[-1].batch
    p.undo_round(batch, "Jamie", "test")
    assert {r.key for r in p.records()} == {"oct.pdf#1", "oct.pdf#2"}
    assert p.decisions()["oct.pdf#2"] == {"skip": "blank"}


def test_accepting_a_repeat_reprints_the_certificate_with_the_whole_month(data):
    import pymupdf
    p = pool(data, [rec("a.pdf", 1, "Ada Example", "Lopez", 30)])          # 74 -> 104: Pencil and Stickers (100)
    p.finish(who="Jamie")
    p = pool(data, [rec("more.jpg", 1, "Ada Example", "Lopez", 400)])      # a late October sheet: 104 -> 504
    assert p.counts()["open"] == 1
    assert "print their October 2026 certificate again" in p.plan().issues[0].message
    p.set_decision("more.jpg#1", {"student": "S0001", "pages": 400, "repeat_ok": True})
    text = p.finish(who="Jamie")
    st = p.log.state()
    assert st.month_pages[("S0001", "2026-10")] == 430 and st.students["S0001"].total == 504
    given = [e.data["levels"] for e in st.events if e.kind == "prizes_given" and e.batch == st.rounds[-1].batch]
    assert given == [[500]]                                                 # only the new prize is bagged
    assert "printed again with all of October 2026 (430 pages)" in text
    doc = pymupdf.open(wsp.Workspace(data).round_folder("2026-10", 2).certificates)
    lists, cert = doc[0].get_text(), doc[1].get_text().replace("\xa0", " ")
    assert "reprint, 430 pp" in lists and "Ribbon" in lists and "Pencil" not in lists
    assert "430" in cert and "100 Pages" in cert and "500 Pages" in cert and "REPLACES EARLIER" in cert
    # redrawing the round from the log gives the same reprint
    runner.reprint(p, st.rounds[-1].batch)
    doc = pymupdf.open(wsp.Workspace(data).round_folder("2026-10", 2).certificates)
    assert "reprint, 430 pp" in doc[0].get_text()
