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
    """The importer leaves blank teachers blank; a person fills them in before they get a certificate."""
    fixed = cp.copy()
    for sid, s in fixed.students.items():
        if not s.teacher:
            fixed.students[sid] = ck.replace(s, teacher=teacher)
    return fixed


def by_name(cp):
    return {s.name: s for s in cp.students.values()}


def test_reads_details_and_ignores_prize_marks(master):
    cp, _ = im.parse_master(master)
    s = by_name(cp)
    assert len(cp.students) == 6 and cp.month is None
    assert (s["Ada Example"].teacher, s["Ada Example"].grade, s["Ada Example"].total) == ("Lopez", "2", 2206)
    assert s["Sam Sample"].total == 12528            # "12528" without a comma reads too
    assert [x.id for x in cp.students.values()] == [f"S000{i}" for i in range(1, 7)]


def test_prize_inventory_table_is_not_imported(master):
    cp, _ = im.parse_master(master)
    assert not any("bookmark" in s.name.lower() for s in cp.students.values())


def test_prizes_come_from_the_change_in_total_so_the_marks_never_matter(master):
    cp, report = im.parse_master(master)
    # Sam Sample has 12,528 pages and x marks only up to 10,000: no prize is "owed" for the old total
    assert not any("no x yet" in line or "marked as given" in line for line in report)
    cp = _with_teacher(cp)
    _, results = ck.apply_month(cp, "October 2026", [ck.Entry("S0003", 100)], TABLE)    # 12,528 -> 12,628
    assert results[0].prizes == ()
    _, results = ck.apply_month(cp, "October 2026", [ck.Entry("S0001", 1000)], TABLE)   # 2,206 -> 3,206
    assert [p[1] for p in results[0].prizes] == ["2,500 Pages"]


def test_report_flags_what_needs_a_person(master):
    _, report = im.parse_master(master)
    text = "\n".join(report)
    assert "Sam Sample: no teacher" in text
    assert "Rae Roe: no previous total" in text
    assert "Ada Example" not in text and "Io Li" not in text      # clean rows; red text is only a note


def test_red_text_is_kept_as_a_note(master):
    cp, _ = im.parse_master(master)
    assert "red" in by_name(cp)["Io Li"].notes and by_name(cp)["Ada Example"].notes == ""


def test_a_child_without_a_teacher_cannot_get_a_certificate_until_fixed(master):
    cp, _ = im.parse_master(master)
    ck.apply_month(cp, "October 2026", [ck.Entry("S0001", 5)], TABLE)          # has a teacher: fine
    with pytest.raises(ck.CheckpointError, match="no teacher"):
        ck.apply_month(cp, "October 2026", [ck.Entry("S0003", 5)], TABLE)      # Sam Sample has none


def test_imported_checkpoint_round_trips_through_xlsx(master, tmp_path):
    cp, _ = im.parse_master(master)
    path = tmp_path / "seed.xlsx"
    ck.save(cp, path)
    assert ck.load(path).students == cp.students


def test_a_pdf_that_is_not_the_master_is_refused(tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "not a master sheet")
    doc.save(tmp_path / "x.pdf")
    with pytest.raises(im.ImportProblem, match="found no student rows"):
        im.parse_master(tmp_path / "x.pdf")


def test_cli_writes_a_checkpoint_and_prints_a_summary(master, tmp_path, capsys):
    out = tmp_path / "work" / "seed.xlsx"
    im.main([str(master), "--out", str(out)])
    printed = capsys.readouterr().out
    assert "Imported 6 students" in printed and "Lopez (1)" in printed and "(none) (1)" in printed
    assert out.exists()


def test_questions_for_staff_lists_red_rows_and_missing_teachers(master, tmp_path):
    cp, report = im.parse_master(master)
    text = im.questions_for_staff(cp, report)
    assert "Io Li - Biello/Tanner - grade K - 74 pages" in text       # the red row
    assert "Sam Sample - grade 4" in text                              # no teacher
    out = tmp_path / "q.txt"
    im.main([str(master), "--out", str(tmp_path / "s.xlsx"), "--questions", str(out)])
    assert out.read_text() == text


def test_teacher_alias_merges_two_labels_into_one_class(master, tmp_path):
    cp, _ = im.parse_master(master, teacher_aliases={"biello/tanner": "Biello"})
    assert by_name(cp)["Io Li"].teacher == "Biello" and by_name(cp)["Ada Example"].teacher == "Lopez"
    out = tmp_path / "s.xlsx"
    im.main([str(master), "--out", str(out), "--teacher-alias", "Biello/Tanner=Biello"])
    assert ck.load(out).students["S0002"].teacher == "Biello"
    with pytest.raises(SystemExit, match="OLD=NEW"):
        im.main([str(master), "--out", str(out), "--teacher-alias", "nonsense"])
