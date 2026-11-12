"""Print PDF: classroom cover pages then certificates. Fake children only."""
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import make_certificates as mc  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import render  # noqa: E402

TABLE = mc.load_levels()


def build(spec):
    """spec: [(name, teacher, previous_total, pages)] -> results for October 2026."""
    cp = ck.Checkpoint()
    entries = []
    for name, teacher, prev, pages in spec:
        s = ck.add_student(cp, name, teacher, total=prev)
        entries.append(ck.Entry(s.id, pages))
    return ck.apply_month(cp, "October 2026", entries, TABLE)[1]


def page_texts(path):
    return [p.get_text().replace("\xa0", " ") for p in pymupdf.open(path)]   # PyMuPDF reports spaces as NBSP


def test_each_classroom_gets_a_cover_then_its_certificates(tmp_path):
    results = build([("Zed Zero", "Park", 0, 30),          # First Pages
                     ("Ada Example", "Park", 74, 30),      # 100 Pages
                     ("Bo Brown", "Lopez", 200, 5)])       # nothing
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    texts = page_texts(out)
    assert len(texts) == 2 + 3                            # 2 covers + 3 certificates
    assert texts[0].startswith("Lopez") and "Bo Brown" in texts[0]
    assert "Beverly Cleary Reading Challenge" in texts[1] and "Bo Brown" in texts[1]   # certificate
    assert texts[2].startswith("Park")
    # certificates inside a classroom are alphabetical, matching the cover list
    assert "Ada Example" in texts[3] and "Zed Zero" in texts[4]
    assert out.exists()


def test_cover_lists_prizes_and_tally(tmp_path):
    results = build([("Ada Example", "Park", 74, 30), ("Io Li", "Park", 0, 30), ("Sam Sample", "Park", 0, 10)])
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    cover = page_texts(out)[0]
    assert "3 students" in cover
    assert "2 × Bookmark" in cover and "1 × Pencil and Stickers" in cover
    assert "October 2026" in cover


def test_cover_for_a_class_with_no_prizes(tmp_path):
    results = build([("Ada Example", "Park", 300, 5)])
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    assert "No prizes this month" in page_texts(out)[0]


def test_big_class_spills_onto_a_second_cover_page(tmp_path):
    results = build([(f"Student {i:02d}", "Park", 0, 150) for i in range(40)])   # each owed 2 prizes
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    texts = page_texts(out)
    covers = [t for t in texts if "(continued)" in t or t.startswith("Park")]
    assert len(texts) == 40 + len(covers) and len(covers) >= 2
    assert "Park (continued)" in texts[1]
    assert all(f"Student {i:02d}" in "".join(texts[:len(covers)]) for i in range(40))   # nobody dropped


def test_prize_with_many_items_wraps_inside_its_row(tmp_path):
    results = build([("Ada Example", "Park", 0, 130000)])     # crosses every level
    out = tmp_path / "print.pdf"
    warnings = render.render_print_pdf(results, "October 2026", out)
    doc = pymupdf.open(out)
    assert doc[0].get_text().count("Trophy") >= 1
    for page in doc:
        assert all(0 <= b[0] and b[2] <= page.rect.width for b in page.get_text("blocks"))


def test_long_names_and_teacher_names_do_not_overflow(tmp_path):
    results = build([("Maximilian Bartholomew Featherstonehaugh-Cholmondeley III", "Alvarez-Hernandez / Montgomery-Whitfield", 0, 30)])
    out = tmp_path / "print.pdf"
    warnings = render.render_print_pdf(results, "October 2026", out)
    page = pymupdf.open(out)[0]
    for b in page.get_text("blocks"):
        assert b[0] >= 0 and b[2] <= page.rect.width + 0.5
    assert any("shrunk" in w for w in warnings)


def test_prize_tally_is_ordered_by_level(tmp_path):
    results = build([("A A", "P", 0, 600), ("B B", "P", 0, 30)])
    assert render.prize_tally(results) == [("Bookmark", 2), ("Pencil and Stickers", 1), ("Ribbon", 1)]


def test_outline_has_one_entry_per_classroom(tmp_path):
    results = build([("A A", "Park", 0, 30), ("B B", "Lopez", 0, 30)])
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    assert [(e[1], e[2]) for e in pymupdf.open(out).get_toc()] == [("Lopez (1)", 1), ("Park (1)", 3)]


def test_all_pages_are_landscape_letter(tmp_path):
    results = build([("A A", "Park", 0, 30), ("B B", "Lopez", 0, 30)])
    out = tmp_path / "print.pdf"
    render.render_print_pdf(results, "October 2026", out)
    assert {(round(p.rect.width), round(p.rect.height)) for p in pymupdf.open(out)} == {(792, 612)}


def test_cut_and_stack_puts_each_class_in_order_after_one_cut():
    from rc import render as rd
    for n in range(1, 30):
        sheets = rd.cut_and_stack(n)
        top_pile = [top for top, _ in sheets]
        bottom_pile = [b for _, b in sheets if b is not None]
        assert top_pile + bottom_pile == list(range(n))           # bottom pile placed under the top pile
        assert len(sheets) == (n + 1) // 2


def test_cutting_in_batches_keeps_the_order():
    """Too many sheets for the cutter: cut a few from the top at a time, top halves onto one pile and bottom halves
    onto another (each batch under the one before), then the bottom pile under the top pile."""
    for n in range(1, 60):
        sheets = render.cut_and_stack(n)
        for batch in range(1, 12):
            top_pile, bottom_pile = [], []
            for start in range(0, len(sheets), batch):
                for top, bottom in sheets[start:start + batch]:
                    top_pile.append(top)
                    bottom_pile.append(bottom)
            order = [i for i in top_pile + bottom_pile if i is not None]
            assert order == list(range(n)), (n, batch)
        assert all(top is not None for top, _ in sheets) and sum(b is None for _, b in sheets) == n % 2
