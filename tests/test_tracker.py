"""Tracker export -> certificate rows. Run with:  python -m pytest -q"""
import csv
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
import tracker  # noqa: E402

TABLE = mc.load_levels()
ROSTER = [("C0001", "Ada Example", "Lopez"), ("C0002", "Sam Sample", "Park"), ("C0003", "Io Li", "Park")]
E_COLS = ["Entered at", "Month", "Pages", "Name on form", "Teacher on form", "Child", "Void (x)", "Child ID",
          "Status", "Pages counted"]
P_COLS = ["Given on", "Child", "Level", "Threshold", "Child ID", "Void (x)"]


def write(path, cols, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)
    return path


def entry(month, pages, cid="", void="", name="x"):
    return ["", month, pages, name, "", "", void, cid, "", 0]


@pytest.fixture
def make(tmp_path):
    def _make(entries, prizes=()):
        r = write(tmp_path / "roster.csv", ["Child ID", "Name", "Teacher"], ROSTER)
        e = write(tmp_path / "entries.csv", E_COLS, entries)
        p = write(tmp_path / "prizes.csv", P_COLS, prizes)
        return r, e, p
    return _make


def build(make, entries, prizes=(), month="October 2026"):
    return tracker.build_rows(*make(entries, prizes), month, TABLE)


def test_totals_run_up_to_the_month_and_owed_levels_are_all_unclaimed_ones(make):
    rows, _, _ = build(make, [entry("2026-09", "2,400", "C0001"), entry("2026-10", 700, "C0001"),
                              entry("2026-11", 9999, "C0001")])
    (r,) = rows
    assert (r["pages"], r["total_pages"]) == ("700", "3100")  # November is not counted yet
    assert r["_resolved"][0] == ["First Pages", "100 Pages", "500 Pages", "1,000 Pages", "2,500 Pages"]


def test_prizes_already_given_are_not_owed_again(make):
    rows, _, _ = build(make, [entry("2026-10", 3100, "C0001")],
                       [["", "Ada", "1,000 Pages", "1,000", "C0001", ""]])
    assert rows[0]["_resolved"][0] == ["2,500 Pages"]


def test_skipped_months_still_owe_earlier_levels(make):
    # nothing handed out in September; October's certificate lists the September levels too
    rows, _, _ = build(make, [entry("2026-09", 120, "C0001"), entry("2026-10", 30, "C0001")])
    assert rows[0]["_resolved"][0] == ["First Pages", "100 Pages"]


def test_latest_row_wins_and_void_is_ignored(make):
    rows, _, _ = build(make, [entry("2026-10", 50, "C0001"), entry("2026-10", 60, "C0001"),
                              entry("2026-10", 5000, "C0001", void="x")])
    assert rows[0]["pages"] == "60"


def test_voided_prize_does_not_count_as_given(make):
    rows, _, _ = build(make, [entry("2026-10", 150, "C0001")],
                       [["", "Ada", "100 Pages", "100", "C0001", "x"]])
    assert rows[0]["_resolved"][0] == ["First Pages", "100 Pages"]


def test_unresolved_entries_are_reported_not_counted(make):
    rows, _, unresolved = build(make, [entry("2026-10", 70, "", name="Reid"),
                                       entry("2026-10", 10, "C9999"), entry("2026-10", "", "C0001"),
                                       ["", "", "", "", "", "", "", "", "", 0]])  # formula-only row
    assert rows == []
    assert [u["problem"] for u in unresolved] == [
        "no child chosen yet", "child id C9999 is not on the roster", "missing month or pages"]
    assert unresolved[0]["name_on_form"] == "Reid"


def test_owed_child_without_entry_this_month_gets_a_note_not_a_certificate(make):
    rows, notes, _ = build(make, [entry("2026-09", 150, "C0001"), entry("2026-10", 20, "C0002")])
    assert [r["name"] for r in rows] == ["Sam Sample"]
    assert any("Ada Example" in n and "100 Pages" in n for n in notes)


def test_month_accepts_either_spelling(make):
    a = build(make, [entry("2026-10", 20, "C0002")], month="2026-10")[0]
    assert a[0]["name"] == "Sam Sample"
    with pytest.raises(ValueError):
        tracker.month_key("Octember")


def test_command_line_end_to_end(make, tmp_path):
    r, e, p = make([entry("2026-10", 3100, "C0001"), entry("2026-10", 70, "", name="Reid")])
    out = tmp_path / "out"
    res = subprocess.run([sys.executable, str(ROOT / "make_certificates.py"), "--roster", r, "--entries", e,
                          "--prizes", p, "--month", "October 2026", "--out", str(out)],
                         capture_output=True, text=True, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "NOT counted" in res.stdout and "Reid" in res.stdout
    assert (out / "Ada_Example.pdf").exists() and (out / "unresolved.csv").exists()
    prizes = list(csv.DictReader(open(out / "prizes.csv")))
    assert prizes[0]["levels"].endswith("2,500 Pages") and prizes[0]["teacher"] == "Lopez"
