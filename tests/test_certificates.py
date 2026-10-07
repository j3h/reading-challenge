"""Run with:  python -m pytest -q"""
import sys
from pathlib import Path

import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import make_certificates as mc  # noqa: E402

TABLE = mc.load_levels()


def row(pages, total, **extra):
    return {"name": "Test Child", "pages": str(pages), "total_pages": str(total),
            "_default_month": "October 2026", **extra}


# ---- which levels a child reaches this month ---------------------------------
def test_levels_crossed_this_month():
    # 5,043 total - 3,194 this month = 1,849 before -> crosses 2,500 and 5,000
    labels, prizes = mc.resolve_levels(row("3,194", "5,043"), TABLE)
    assert labels == ["2,500 Pages", "5,000 Pages"]
    assert prizes == ["Pen", "Metal Bookmark"]


def test_first_month_crosses_everything_up_to_total():
    labels, _ = mc.resolve_levels(row(5120, 5120), TABLE)
    assert labels[0] == "First Pages" and labels[-1] == "5,000 Pages" and len(labels) == 6


def test_threshold_boundaries():
    # reaching a level exactly counts; having already been exactly on it does not
    assert mc.resolve_levels(row(50, 100), TABLE)[0] == ["100 Pages"]
    assert mc.resolve_levels(row(50, 150), TABLE)[0] == []


def test_no_new_level():
    assert mc.resolve_levels(row(50, 300), TABLE) == ([], [])


def test_total_smaller_than_month_is_an_error():
    with pytest.raises(ValueError):
        mc.resolve_levels(row(500, 100), TABLE)


def test_manual_override_replaces_computed_levels():
    labels, prizes = mc.resolve_levels(row(500, 100000, prize_levels="Special Award; Other"), TABLE)
    assert labels == ["Special Award", "Other"] and prizes == []


# ---- layout ------------------------------------------------------------------
def render(name, pages, total, labels):
    doc = pymupdf.open()
    r = row(pages, total)
    r["name"] = name
    mc.draw_certificate(doc, r, labels, lambda msg: None)
    lines = []
    for b in doc[0].get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            x0, _, x1, _ = l["bbox"]
            lines.append({"cx": (x0 + x1) / 2, "w": x1 - x0, "base": l["spans"][0]["origin"][1],
                          "text": "".join(s["text"] for s in l["spans"])})
    return lines


CASES = [
    ("Juniper Albright", 3194, 5043, ["2,500 Pages", "5,000 Pages"]),
    ("Io Li", 1, 1, ["First Pages"]),
    ("Ada Okonkwo-Reyes", 5120, 5120, [lv for _, lv, _ in TABLE[:6]]),
    ("Bartholomew Maximilian Featherstonehaugh-Wellington", 812, 2600, ["2,500 Pages"]),
    ("Tess Moriarty", 50, 300, []),
]


@pytest.mark.parametrize("name,pages,total,labels", CASES)
def test_every_line_is_centred_and_inside_the_panel(name, pages, total, labels):
    for ln in render(name, pages, total, labels):
        assert abs(ln["cx"] - mc.CENTER_X) < 0.5, ln
        # The fixed title is wider than MAX_WIDTH by design (it matches the Canva layout);
        # MAX_WIDTH governs everything that varies per child.
        if ln["text"] != mc.TITLE:
            assert ln["w"] <= mc.MAX_WIDTH + 0.5, ln
        assert ln["base"] <= mc.BOTTOM_LIMIT + 0.01, ln


def test_prize_names_are_never_split_across_lines():
    lines = render("Ada Okonkwo-Reyes", 5120, 5120, [lv for _, lv, _ in TABLE[:6]])
    text = "\n".join(l["text"] for l in lines)
    for _, label, _ in TABLE[:6]:
        assert label in text, f"{label!r} was broken across lines"


def test_singular_page_wording():
    text = " ".join(l["text"] for l in render("Io Li", 1, 1, ["First Pages"]))
    assert "1 Page" in text and "1 page read" in text
