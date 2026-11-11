"""The event log: replay, corrections, duplicates, two Macs, the workbook as opening events. Fake children only."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import events as ev  # noqa: E402

TABLE = mc.load_levels()
SCAN = {"scan": "oct.pdf", "page": 3}
MANUAL = {"manual": True}


def E(kind, time, who="Jamie", reason="", sources=(SCAN,), **data):
    return ev.Event(kind, data, who, reason, tuple(sources), f"2026-11-{time}T10:00:00+00:00")


def added(sid="S1", name="Ada Example", teacher="Lopez", time="01"):
    return E("student_added", time, student=sid, name=name, teacher=teacher, grade="2", sources=())


def test_replay_builds_totals_teachers_and_history():
    st = ev.replay([added(), E("pages_recorded", "02", student="S1", month="2026-10", pages=120),
                    E("teacher_set", "03", student="S1", teacher="Park", sources=()),
                    E("pages_recorded", "04", student="S1", month="2026-11", pages=30)])
    s = st.students["S1"]
    assert (s.total, s.teacher) == (150, "Park") and not st.warnings
    assert [(h.month, h.pages, h.total_after, h.source) for h in st.history] == \
        [("2026-10", 120, 120, "oct.pdf p. 3"), ("2026-11", 30, 150, "oct.pdf p. 3")]
    assert st.to_checkpoint().month == "2026-11"


def test_events_apply_in_time_order_whatever_order_they_are_read():
    a = [added(), E("teacher_set", "05", student="S1", teacher="Biello", sources=()),
         E("teacher_set", "03", student="S1", teacher="Barry", sources=())]
    assert ev.replay(a).students["S1"].teacher == "Biello" == ev.replay(a[::-1]).students["S1"].teacher


def test_a_cancelled_event_no_longer_counts_but_stays_in_the_history():
    wrong = E("pages_recorded", "02", student="S1", month="2026-10", pages=880)
    st = ev.replay([added(), wrong, E("event_voided", "03", reason="other Luca's sheet", sources=(MANUAL,),
                                      event=wrong.id)])
    assert st.students["S1"].total == 0 and wrong.id in st.voided
    assert [e.kind for e in st.by_student["S1"]] == ["student_added", "pages_recorded"]


def test_the_same_evidence_counts_once_but_two_manual_corrections_count_twice():
    rec1 = E("pages_recorded", "02", student="S1", month="2026-10", pages=100)
    rec2 = E("pages_recorded", "05", who="Josh", student="S1", month="2026-10", pages=100)   # same sheet again
    fix = lambda t: E("pages_adjusted", t, reason="miscounted a book", sources=(MANUAL,), student="S1",
                      month="2026-10", delta=1)
    st = ev.replay([added(), rec1, rec2, fix("06"), fix("07")])
    assert st.students["S1"].total == 102 and [d.id for d, _ in st.duplicates] == [rec2.id]


def test_setting_a_value_back_is_never_mistaken_for_a_duplicate():
    t = lambda time, who: E("teacher_set", time, student="S1", teacher=who)
    st = ev.replay([added(), t("02", "Barry"), t("03", "Biello"), t("04", "Barry")])
    assert st.students["S1"].teacher == "Barry" and not st.duplicates


def test_invalid_events_are_reported_not_applied():
    st = ev.replay([E("pages_recorded", "02", student="S9", month="2026-10", pages=5), added(),
                    E("pages_adjusted", "03", reason="x", student="S1", month="2026-10", delta=-5)])
    assert st.students["S1"].total == 0 and len(st.warnings) == 2
    assert "no student S9" in st.warnings[0] and "below 0" in st.warnings[1]


def test_prizes_owed_are_levels_crossed_but_never_handed_out():
    st = ev.replay([added(), E("pages_recorded", "02", student="S1", month="2026-10", pages=600),
                    E("prizes_given", "03", student="S1", month="2026-10", levels=[1, 100])])
    assert [lv[0] for lv in st.owed(TABLE)["S1"]] == [500]


# ------------------------------------------------------------------ the files
def test_each_mac_writes_its_own_file_and_reading_merges_them(tmp_path):
    jamie, josh = ev.EventLog(tmp_path, "Jamies-Mac"), ev.EventLog(tmp_path, "Joshs-Mac")
    jamie.append([added()])
    josh.append([E("pages_recorded", "02", who="Josh", student="S1", month="2026-10", pages=40)])
    jamie.append([E("pages_recorded", "03", student="S1", month="2026-10", pages=2, sources=({"photo": "x.jpg"},))])
    assert sorted(p.name for p in jamie.files()) == ["Jamies-Mac.jsonl", "Joshs-Mac.jsonl"]
    assert jamie.state().students["S1"].total == josh.state().students["S1"].total == 42
    events = [json.loads(line) for line in (tmp_path / "events" / "Jamies-Mac.jsonl").read_text().splitlines()]
    assert [e["device"] for e in events] == ["Jamies-Mac", "Jamies-Mac"]


def test_append_refuses_bad_events_and_writes_nothing(tmp_path):
    log = ev.EventLog(tmp_path, "mac")
    log.append([added()])
    before = log.own_file.read_text()
    for bad, msg in ((E("pages_recorded", "02", student="S9", month="2026-10", pages=5), "no student S9"),
                     (E("pages_adjusted", "02", student="S1", month="2026-10", delta=3), "needs a reason"),
                     (E("pages_recorded", "02", who="", student="S1", month="2026-10", pages=5), "who made it"),
                     (E("pages_recorded", "02", student="S1", month="2026-10", pages=-5), "0 or more"),
                     (E("frobnicate", "02", student="S1"), "unknown event kind")):
        with pytest.raises(ev.EventError, match=msg):
            log.append([bad])
    assert log.own_file.read_text() == before


def test_recording_the_same_sheet_again_writes_nothing_new(tmp_path):
    log = ev.EventLog(tmp_path, "mac")
    rec = lambda: E("pages_recorded", "02", student="S1", month="2026-10", pages=100)
    log.append([added(), rec()])
    log.append([rec()])
    assert len(log.read()) == 2 and log.state().students["S1"].total == 100


def test_compact_drops_only_duplicates(tmp_path):
    a, b = ev.EventLog(tmp_path, "a"), ev.EventLog(tmp_path, "b")
    a.append([added(), E("pages_recorded", "02", student="S1", month="2026-10", pages=100)])
    # b recorded the same sheet before it had synced a's events
    b.own_file.parent.mkdir(exist_ok=True)
    b.own_file.write_text(ev.Event("pages_recorded", {"student": "S1", "month": "2026-10", "pages": 100}, "Josh", "",
                                   (SCAN,), "2026-11-03T10:00:00+00:00", "b").to_json() + "\n")
    assert a.state().students["S1"].total == 100
    assert b.compact() == 1 and a.compact() == 0
    assert b.own_file.read_text().strip() == ""
    assert a.state().students["S1"].total == 100 and len(a.read()) == 2


def test_two_macs_setting_different_teachers_is_reported(tmp_path):
    st = ev.replay([added(),
                    ev.Event("teacher_set", {"student": "S1", "teacher": "Barry"}, "Jamie", "", (), "2026-11-02T10:02:00+00:00", "a"),
                    ev.Event("teacher_set", {"student": "S1", "teacher": "Biello"}, "Josh", "", (), "2026-11-02T10:05:00+00:00", "b")])
    assert st.students["S1"].teacher == "Biello" and "within a day" in st.warnings[0]


def test_conflict_copies_and_damaged_lines_are_caught(tmp_path):
    log = ev.EventLog(tmp_path, "mac")
    log.append([added()])
    for copy in ("mac 2.jsonl", "mac (1).jsonl", "mac (Jamie's conflicted copy 2026-11-11).jsonl"):
        (tmp_path / "events" / copy).write_text(log.own_file.read_text())
        assert any(copy in p and "copy made by the sync service" in p for p in log.problems())
        assert log.state().students["S1"].name == "Ada Example" and len(log.state().events) == 1   # read, counted once
        (tmp_path / "events" / copy).unlink()
    assert log.problems() == []
    (tmp_path / "events" / "mac 2.jsonl").write_text("{not json\n")
    with pytest.raises(ev.EventError, match="damaged"):
        log.read()


def test_files_still_in_icloud_block_writing(tmp_path):
    log = ev.EventLog(tmp_path, "mac")
    log.append([added()])
    (tmp_path / "events" / ".other.jsonl.icloud").write_bytes(b"")
    with pytest.raises(ev.EventError, match="downloading"):
        log.append([E("pages_recorded", "02", student="S1", month="2026-10", pages=5)])


# ------------------------------------------------------------------ the workbook as opening events
def test_opening_events_replay_to_the_same_totals_as_the_import(tmp_path):
    from test_import_workbook import workbook as make_workbook  # noqa: F401  (fixture function)
    from rc import import_workbook as iw
    path = make_workbook.__wrapped__(tmp_path) if hasattr(make_workbook, "__wrapped__") else None
    if path is None:
        pytest.skip("fixture not callable directly")
    cp, _ = iw.parse_workbook(path, "September 2026")
    events, _ = iw.opening_events(path, "September 2026", "Josh")
    st = ev.replay(events)
    assert not st.warnings
    assert {s.name: s.total for s in st.students.values()} == {s.name: s.total for s in cp.students.values()}
    io = next(s.id for s in st.students.values() if s.name == "Io Li")
    kinds = [(e.kind, e.data.get("month"), e.data.get("pages", e.data.get("total"))) for e in st.by_student[io]]
    assert kinds[:4] == [("student_added", None, None), ("total_set", None, 2000), ("pages_recorded", "2026-05", 400),
                         ("pages_recorded", "2026-07", 100)]
    assert all(src.get("workbook") == "master.xlsx" for e in st.by_student[io] if e.kind == "pages_recorded"
               for src in e.sources)
    assert ev.replay(events + events).students[io].total == 2800          # importing twice changes nothing
    assert not st.owed(TABLE)                                              # everything so far counts as handed out


def test_adjust_and_history_from_the_command_line(tmp_path, capsys):
    from rc import __main__ as cli
    log = ev.EventLog(tmp_path)
    log.append([added()])
    run = lambda *a: cli.main(["--data", str(tmp_path), *a])
    run("adjust", "--who", "Jamie", "--student", "Ada Example", "--month", "September 2026", "--pages", "20",
        "--reason", "miscounted")
    run("adjust", "--who", "Jamie", "--student", "S1", "--teacher", "Park", "--snapshot", str(tmp_path / "c.xlsx"))
    assert "Recorded: September 2026: pages +20 - miscounted" in capsys.readouterr().out
    assert ck.load(tmp_path / "c.xlsx").students["S1"].total == 20
    with pytest.raises(SystemExit, match="needs a reason|pages_adjusted needs a reason"):
        run("adjust", "--who", "Jamie", "--student", "S1", "--month", "2026-09", "--pages", "5")
    run("history", "Ada Example")
    out = capsys.readouterr().out
    assert "Ada Example (S1), Park" in out and "pages +20 - miscounted" in out and "teacher set to Park" in out
