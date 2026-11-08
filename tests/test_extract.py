"""Reading scans with Claude: answers -> records, caching, failures. A fake client stands in for the API."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rc import __main__ as cli  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import extract as ex  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import workspace as wsp  # noqa: E402


def answer(name="Ada Example", teacher="Lopez", month="October", total=30, rows=(), others=(), **kw):
    a = {"is_reading_sheet": True, "name": name, "name_confidence": "high", "teacher": teacher,
         "teacher_confidence": "high", "month": month, "has_total": total is not None,
         "total_pages": total or 0, "total_confidence": "high", "total_where": "in the box",
         "other_total_candidates": list(others),
         "rows": [{"title": t, "pages": p} for t, p in rows], "note": ""}
    a.update(kw)
    return a


class FakeClient:
    """Answers by the sheet's number: the test PDF has 'sheet N' text, but the model only sees an image, so
    answers are handed out in call order per page via `by_call`."""

    def __init__(self, answers, stop_reason="end_turn", fail_on=()):
        self.answers = list(answers)
        self.calls = []
        self.stop_reason = stop_reason
        self.fail_on = set(fail_on)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        i = len(self.calls)
        self.calls.append(kw)
        if i in self.fail_on:
            raise RuntimeError("boom")
        block = SimpleNamespace(type="text", text=json.dumps(self.answers[i % len(self.answers)]))
        return SimpleNamespace(content=[block], stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200))


def make_scan(folder, pages=3):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=612, height=792).insert_text((72, 100), f"sheet {i + 1}", fontsize=20)
    path = Path(folder) / "scan.pdf"
    doc.save(path)
    return path


@pytest.fixture
def month(tmp_path):
    mf = wsp.Workspace(tmp_path).month_folder("October 2026").create()
    make_scan(mf.scans)
    return mf


def test_page_ranges():
    assert ex.parse_page_range(None, 4) == [1, 2, 3, 4]
    assert ex.parse_page_range("1-3", 10) == [1, 2, 3]
    assert ex.parse_page_range("2,5-6", 10) == [2, 5, 6]
    with pytest.raises(ck.CheckpointError, match="outside this scan"):
        ex.parse_page_range("9", 3)
    with pytest.raises(ck.CheckpointError, match="cannot read page range"):
        ex.parse_page_range("a-b", 3)


def test_clear_answer_becomes_a_confident_record():
    r = ex.to_record("scan.pdf", 4, answer(total=158))
    assert (r.file, r.page, r.name, r.teacher, r.month, r.pages) == ("scan.pdf", 4, "Ada Example", "Lopez", "October", 158)
    assert r.confidence == rv.HIGH and r.pages_confidence == rv.HIGH and r.note == ""


def test_conflicting_totals_are_low_confidence_and_say_so():
    r = ex.to_record("s.pdf", 1, answer(total=158, others=[2300]))
    assert r.pages == 158 and r.pages_confidence == rv.LOW and "two totals: 158 vs 2,300" in r.note


def test_the_written_total_is_used_even_when_rows_disagree():
    r = ex.to_record("s.pdf", 1, answer(total=100, rows=[("A", 30), ("B", 40)]))
    assert r.pages == 100 and r.pages_confidence == rv.HIGH and r.note == ""


def test_no_total_falls_back_to_the_rows_but_asks_a_person():
    r = ex.to_record("s.pdf", 1, answer(total=None, rows=[("A", 30), ("B", 40)]))
    assert r.pages == 70 and r.pages_confidence == rv.LOW and "added up the rows" in r.note


def test_no_total_and_no_rows_is_unreadable():
    r = ex.to_record("s.pdf", 1, answer(total=None))
    assert r.pages is None and r.pages_confidence == rv.UNREADABLE


def test_low_name_confidence_and_blank_name_lower_the_identity_confidence():
    assert ex.to_record("s.pdf", 1, answer(name_confidence="low")).confidence == rv.LOW
    assert ex.to_record("s.pdf", 1, answer(name="", name_confidence="high")).confidence == rv.UNREADABLE
    assert ex.to_record("s.pdf", 1, answer(teacher="", teacher_confidence="unreadable")).confidence == rv.HIGH


def test_pages_that_are_not_sheets_are_dropped():
    assert ex.to_record("s.pdf", 1, answer(is_reading_sheet=False)) is None


def test_extract_month_reads_every_page_and_writes_records(month):
    client = FakeClient([answer("Ada Example", total=30), answer("Io Li", total=5), answer(is_reading_sheet=False)])
    records, dropped, usage = ex.extract_month(client, month, log=lambda *_: None, workers=1)
    assert [r.name for r in records] == ["Ada Example", "Io Li"] and dropped == ["scan.pdf p. 3"]
    assert usage == {"input_tokens": 4500, "output_tokens": 600, "pages_sent": 3}
    assert rv.load_records(month.work / "records.json") == records
    call = client.calls[0]
    assert call["model"] == ex.DEFAULT_MODEL and call["output_config"]["format"]["type"] == "json_schema"
    image = call["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"


def test_nothing_is_asked_twice(month):
    client = FakeClient([answer()])
    ex.extract_month(client, month, log=lambda *_: None, workers=1)
    assert len(client.calls) == 3
    again = FakeClient([answer("Someone Else")])
    records, _, usage = ex.extract_month(again, month, log=lambda *_: None)
    assert again.calls == [] and usage["pages_sent"] == 0 and {r.name for r in records} == {"Ada Example"}
    forced = FakeClient([answer("Someone Else")])
    records, _, _ = ex.extract_month(forced, month, force=True, log=lambda *_: None, workers=1)
    assert len(forced.calls) == 3 and {r.name for r in records} == {"Someone Else"}


def test_page_slice_only_sends_those_pages(month):
    client = FakeClient([answer()])
    records, _, _ = ex.extract_month(client, month, pages="2", log=lambda *_: None)
    assert len(client.calls) == 1 and [r.page for r in records] == [2]


def test_a_failed_page_saves_no_records_and_a_rerun_only_retries_that_page(month):
    flaky = FakeClient([answer()], fail_on={1})
    with pytest.raises(ex.ExtractionError, match=r"(?s)1 page\(s\) could not be read.*scan.pdf p. 2.*boom"):
        ex.extract_month(flaky, month, log=lambda *_: None, workers=1)
    assert not (month.work / "records.json").exists()
    fixed = FakeClient([answer()])
    records, _, _ = ex.extract_month(fixed, month, log=lambda *_: None, workers=1)
    assert len(fixed.calls) == 1 and len(records) == 3        # only the failed page was sent again


def test_a_refusal_or_cutoff_is_a_failure_not_a_blank_sheet(month):
    for reason in ("refusal", "max_tokens"):
        with pytest.raises(ex.ExtractionError, match="could not be read"):
            ex.extract_month(FakeClient([answer()], stop_reason=reason), month, force=True, log=lambda *_: None,
                             workers=1)


def test_no_scans_and_icloud_placeholders_are_clear_errors(tmp_path):
    mf = wsp.Workspace(tmp_path).month_folder("October 2026").create()
    with pytest.raises(ck.CheckpointError, match="no scans"):
        ex.extract_month(FakeClient([answer()]), mf)
    make_scan(mf.scans)
    (mf.scans / ".other.pdf.icloud").write_bytes(b"")
    with pytest.raises(ck.CheckpointError, match="still in iCloud"):
        ex.extract_month(FakeClient([answer()]), mf)


def test_image_scans_are_read_too(tmp_path):
    mf = wsp.Workspace(tmp_path).month_folder("October 2026").create()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    pix.save(mf.scans / "sheet1.png")
    records, _, _ = ex.extract_month(FakeClient([answer("Ada Example")]), mf, log=lambda *_: None)
    assert [(r.file, r.page) for r in records] == [("sheet1.png", 1)]


def test_dry_run_counts_pages_without_calling_anything(month):
    count, total, largest, done = ex.dry_run(month)
    assert count == 3 and total >= largest > 0 and done == 0
    cli.main(["--data", str(month.root), "extract", "--month", "October 2026", "--dry-run"])


def test_cost_estimate_uses_the_model_price():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "pages_sent": 51}
    assert "$6.00" in ex.cost_estimate(usage, "claude-opus-5-5")      # 4.00 + 2.00
    assert "tokens" in ex.cost_estimate(usage, "some-other-model")


def test_extracted_records_flow_into_review_and_a_finished_month(tmp_path):
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)
    start = tmp_path / "2026-27" / "start"
    start.mkdir(parents=True)
    ck.save(cp, start / "checkpoint.xlsx")
    mf = wsp.Workspace(tmp_path).month_folder("October 2026").create()
    make_scan(mf.scans, pages=2)
    ex.extract_month(FakeClient([answer("Ada Example", "Lopez", total=30),
                                 answer("Newcomer Nia", "Moren", total=40, name_confidence="low")]), mf,
                     log=lambda *_: None, workers=1)
    with pytest.raises(SystemExit):
        cli.main(["--data", str(tmp_path), "check", "--month", "October 2026"])          # Nia needs a decision
    cli.main(["--data", str(tmp_path), "decide", "--month", "October 2026", "scan.pdf#2",
              "--new-name", "Nia Newcomer", "--teacher", "Moren"])
    cli.main(["--data", str(tmp_path), "finish", "--month", "October 2026"])
    names = {s.name: s.total for s in ck.load(mf.checkpoint).students.values()}
    assert names == {"Ada Example": 104, "Nia Newcomer": 40}


def test_where_each_value_was_read_is_kept_and_bad_boxes_are_dropped():
    a = answer(boxes={"name": {"x0": 600, "y0": 50, "x1": 900, "y1": 90},
                      "teacher": {"x0": 0, "y0": 0, "x1": 0, "y1": 0},            # not on the page
                      "month": {"x0": 900, "y0": 10, "x1": 1200, "y1": 5},        # nonsense
                      "total": {"x0": 700, "y0": 850, "x1": 800, "y1": 890}})
    r = ex.to_record("s.pdf", 1, a)
    assert r.boxes == {"name": [600, 50, 900, 90], "total": [700, 850, 800, 890]}
    assert ex.to_record("s.pdf", 1, answer()).boxes == {}          # answers saved before boxes existed
    assert set(ex.SCHEMA["properties"]["boxes"]["required"]) == {"name", "teacher", "month", "total"}
