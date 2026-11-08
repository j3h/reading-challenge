"""Master spreadsheet PDF -> first checkpoint. Builds a small fake master with the real layout."""
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import import_master as im  # noqa: E402

TABLE = mc.load_levels()
RED = (1, 0, 0)

# (name, teacher, grade, total, thresholds with an x, red?)
STUDENTS = [
    ("Ada Example", "Lopez", "2", "2,206", [1, 100, 500, 1000], False),
    ("Io Li", "Biello/Tanner", "K", "74", [1], True),
    ("Sam Sample", "", "4", "12528", [1, 100, 500, 1000, 2500, 5000, 7500, 10000], False),   # no teacher
    ("Rae Roe", "Park", "3", "", [], False),                                                  # no total
    ("Bo Brown", "Park", "3", "300", [1, 100, 50000], False),   # 50,000 is not a level in levels.csv
    ("Cy Late", "Park", "1", "2,280", [1, 100, 500, 1000, 2500], False),   # marked past their total
]
EARLY = [(430, "first pages - bookmark", 1), (452, "100 - pencil", 100), (475, "500 - ribbon", 500),
         (497, "1000 - water bottle", 1000), (520, "2500 - pen", 2500), (542, "5000 - Metal Bkmrk", 5000)]
LATE = [(229, "7500 - B&R Scoop", 7500), (251, "10,000 - BCS Shirt", 10000), (274, "70,000 - lunch", 70000),
        (296, "00 - $10 Puddle Town", 85000), (319, "90,000 - eng bkmrk", 90000), (341, "50,000 - trophy", 50000)]
FIRST_Y = 160


def make_master(path, students=STUDENTS, scramble_late_names=False):
    doc = pymupdf.open()

    def text(page, x, y, s, color=(0, 0, 0), rotate=0):
        page.insert_text((x, y), s, fontsize=9, color=color, rotate=rotate)

    def names(page, rows):
        for i, st in enumerate(rows):
            nm = st[0] if not (scramble_late_names and page.number == 4 and i == 0) else "Someone Else"
            text(page, 53, FIRST_Y + 14 * i + 8, nm)

    # group 1: details page + an inventory page
    p = doc.new_page(width=612, height=792)
    text(p, 224, 145, "Teacher"); text(p, 306, 145, "25/26 Grade"); text(p, 391, 145, "Total")
    for i, (nm, teacher, grade, total, _, red) in enumerate(students):
        y = FIRST_Y + 14 * i + 8
        colour = RED if red else (0, 0, 0)
        text(p, 53, y, nm)
        if teacher:
            text(p, 224, y, teacher, colour)
        text(p, 334, y, grade, colour)
        if total:
            text(p, 427 - pymupdf.get_text_length(total, fontsize=9), y, total)
    inv = doc.new_page(width=612, height=792)
    text(inv, 224, 145, "Teacher")
    text(inv, 53, 288, "Prize"); text(inv, 248, 288, "Level")
    text(inv, 53, 302, "paper bookmark"); text(inv, 224, 302, "first pages")

    for group, cols in ((EARLY, EARLY), (LATE, LATE)):
        page = doc.new_page(width=612, height=792)
        for x, label, _ in cols:
            page.insert_text((x + 9, 157), label, fontsize=9, rotate=90)
        names(page, students)
        for i, st in enumerate(students):
            for x, _, thr in cols:
                if thr in st[4]:
                    text(page, x, FIRST_Y + 14 * i + 8, "x")
        doc.new_page(width=612, height=792)     # the marks group's inventory-page counterpart
    doc.save(path)
    return path


@pytest.fixture
def master(tmp_path):
    return make_master(tmp_path / "master.pdf")


def _with_teacher(cp, teacher="Lopez"):
    """The importer leaves blank teachers blank; a person fills them in before the first month."""
    fixed = cp.copy()
    for sid, s in fixed.students.items():
        if not s.teacher:
            fixed.students[sid] = ck.replace(s, teacher=teacher)
    return fixed


def by_name(cp):
    return {s.name: s for s in cp.students.values()}


def test_reads_details_and_marks(master):
    cp, _ = im.parse_master(master, TABLE)
    s = by_name(cp)
    assert len(cp.students) == 6 and cp.month is None
    assert (s["Ada Example"].teacher, s["Ada Example"].grade, s["Ada Example"].total) == ("Lopez", "2", 2206)
    assert s["Ada Example"].last_level == 1000
    assert s["Sam Sample"].total == 12528 and s["Sam Sample"].last_level == 10000   # late group read too
    assert [x.id for x in cp.students.values()] == [f"S000{i}" for i in range(1, 7)]


def test_prize_inventory_table_is_not_imported(master):
    cp, _ = im.parse_master(master, TABLE)
    assert not any("bookmark" in s.name.lower() for s in cp.students.values())


def test_clipped_header_is_resolved_and_unknown_levels_are_ignored(master):
    cp, report = im.parse_master(master, TABLE)
    assert by_name(cp)["Bo Brown"].last_level == 100       # the 50,000 mark does not count
    assert any("50,000 - trophy" in line and "ignored" in line for line in report)
    assert not any("Puddle" in line for line in report)     # '00 - $10 ...' was resolved to 85,000


def test_report_flags_what_needs_a_person(master):
    _, report = im.parse_master(master, TABLE)
    text = "\n".join(report)
    assert "Sam Sample: no teacher" in text
    assert "Rae Roe: no previous total" in text
    assert "Cy Late: marked as given up to 2,500 pages but the total is only 2,280" in text
    assert "Ada Example: total 2,206 reaches" not in text     # marks and total agree
    assert "Io Li" not in text                                 # clean row, red text is only a note


def test_owed_but_unmarked_prizes_are_reported(master):
    cp, report = im.parse_master(master, TABLE)
    # 12,528 pages but the last x is at 10,000: the 12,500 prize was never marked as given
    assert any(line.startswith("Sam Sample: total 12,528 reaches 12,500 Pages") for line in report)
    _, results = ck.apply_month(_with_teacher(cp), "October 2026", [ck.Entry("S0003", 0)], TABLE)
    assert [p[1] for p in results[0].prizes] == ["12,500 Pages"]


def test_red_text_is_kept_as_a_note(master):
    cp, _ = im.parse_master(master, TABLE)
    assert "red" in by_name(cp)["Io Li"].notes and by_name(cp)["Ada Example"].notes == ""


def test_missing_teacher_blocks_the_first_month_until_fixed(master):
    cp, _ = im.parse_master(master, TABLE)
    with pytest.raises(ck.CheckpointError, match="no teacher"):
        ck.apply_month(cp, "October 2026", [], TABLE)


def test_imported_checkpoint_round_trips_through_xlsx(master, tmp_path):
    cp, _ = im.parse_master(master, TABLE)
    path = tmp_path / "seed.xlsx"
    ck.save(cp, path)
    assert ck.load(path).students == cp.students


def test_groups_that_disagree_are_refused(tmp_path):
    path = make_master(tmp_path / "bad.pdf", scramble_late_names=True)
    with pytest.raises(im.ImportProblem, match="do not line up"):
        im.parse_master(path, TABLE)


def test_unexpected_layout_is_refused(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "not a master sheet")
    doc.save(tmp_path / "x.pdf")
    with pytest.raises(im.ImportProblem, match="expected three equal groups"):
        im.parse_master(tmp_path / "x.pdf", TABLE)


def test_cli_writes_a_checkpoint_and_prints_a_summary(master, tmp_path, capsys):
    out = tmp_path / "work" / "seed.xlsx"
    im.main([str(master), "--out", str(out)])
    printed = capsys.readouterr().out
    assert "Imported 6 students" in printed and "Lopez (1)" in printed and "(none) (1)" in printed
    assert out.exists()
