"""The review screen's backend and its HTTP wrapper. Fake children only."""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rc import checkpoint as ck  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import review_web as web  # noqa: E402
from rc import runner  # noqa: E402
from rc import workspace as wsp  # noqa: E402


@pytest.fixture
def month(tmp_path):
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)               # S0001
    ck.add_student(cp, "Rosalind Cox", "Tracy", "3", total=2140)             # S0002
    start = tmp_path / "2026-27" / "start"
    start.mkdir(parents=True)
    ck.save(cp, start / "checkpoint.xlsx")
    m = runner.Month(wsp.Workspace(tmp_path), "October 2026")
    m.folder.create()
    doc = pymupdf.open()
    for i in range(3):
        doc.new_page(width=612, height=792).insert_text((72, 100), f"<script>sheet {i + 1}</script>", fontsize=18)
    doc.save(m.folder.scans / "scan.pdf")
    rv.save_records([
        rv.SheetRecord("scan.pdf", 1, "Ada Example", "Lopez", "October", 30),
        rv.SheetRecord("scan.pdf", 2, "Rozzie", "Tracy", "September", 600, pages_confidence=rv.LOW,
                       note="<b>two totals</b>"),
        rv.SheetRecord("scan.pdf", 3, "Zed Newcomer", "Moren", "October", 40),
    ], m.records_path)
    return m


def by_key(state):
    return {s["key"]: s for s in state["sheets"]}


def test_state_lists_every_sheet_with_status_candidates_and_roster(month):
    st = web.ReviewApp(month).state()
    sheets = by_key(st)
    assert st["month"] == "October 2026" and st["ready"] is False
    assert st["counts"] == {"open": 2, "auto": 1, "decided": 0, "skipped": 0} and st["acceptable"] == 1
    assert sheets["scan.pdf#1"]["status"] == "auto" and "Ada Example" in sheets["scan.pdf#1"]["outcome"]
    rozzie = sheets["scan.pdf#2"]
    assert rozzie["status"] == "open" and rozzie["can_accept"]
    assert rozzie["warnings"] == ["sheet says 'September'; counted for October 2026"]
    assert rozzie["form"] == {"mode": "student", "student": "S0002", "new": {"name": "Rozzie", "grade": ""},
                              "teacher": "Tracy", "pages": 600, "remember_name": True, "duplicate": ""}
    zed = sheets["scan.pdf#3"]["form"]
    assert zed["mode"] == "new" and zed["new"]["name"] == "Zed Newcomer" and zed["teacher"] == ""
    assert not sheets["scan.pdf#3"]["can_accept"]        # 'Moren' is not a teacher on the roster yet
    assert rozzie["candidates"][0]["name"] == "Rosalind Cox" and rozzie["candidates"][0]["reason"].startswith("nickname")
    assert st["teachers"] == ["Lopez", "Tracy"] and [p["name"] for p in st["roster"]] == ["Ada Example", "Rosalind Cox"]
    assert [s["key"] for s in st["sheets"]] == ["scan.pdf#1", "scan.pdf#2", "scan.pdf#3"]


def test_deciding_saves_and_settles_and_finishing_writes_the_month(month):
    app = web.ReviewApp(month)
    app.decide("scan.pdf#2", {"student": "S0002", "remember_name": True, "pages": 600})
    st = app.decide("scan.pdf#3", {"new_student": {"name": "Zed Newcomer", "teacher": "Moren", "grade": "1"}})
    assert st["ready"] is True and st["counts"] == {"open": 0, "auto": 1, "decided": 2, "skipped": 0}
    assert json.loads(month.decisions_path.read_text())["scan.pdf#2"]["student"] == "S0002"
    done = app.finish()
    assert done["finished"] is True and "Certificates: 3" in done["summary"]
    new = ck.load(month.folder.checkpoint)
    assert new.students["S0002"].nicknames == "Rozzie" and new.students["S0002"].total == 2740
    assert {s.name for s in new.students.values()} == {"Ada Example", "Rosalind Cox", "Zed Newcomer"}
    assert web.ReviewApp(month).state()["finished"] is True


def test_an_empty_decision_clears_it_and_unknown_keys_are_dropped(month):
    app = web.ReviewApp(month)
    app.decide("scan.pdf#3", {"skip": "blank", "evil": "ignored", "__proto__": 1})
    assert json.loads(month.decisions_path.read_text()) == {"scan.pdf#3": {"skip": "blank"}}
    st = app.decide("scan.pdf#3", {})
    assert by_key(st)["scan.pdf#3"]["status"] == "open" and json.loads(month.decisions_path.read_text()) == {}
    with pytest.raises(ck.CheckpointError, match="no sheet"):
        app.decide("scan.pdf#99", {"skip": "x"})


def test_skipped_sheets_are_counted_and_do_not_block_finishing(month):
    app = web.ReviewApp(month)
    app.decide("scan.pdf#2", {"skip": "wrong month"})
    st = app.decide("scan.pdf#3", {"skip": "blank"})
    assert st["counts"] == {"open": 0, "auto": 1, "decided": 0, "skipped": 2} and st["ready"] is True


def test_finishing_with_nothing_settled_is_not_ready(month):
    app = web.ReviewApp(month)
    st = app.state()
    assert st["ready"] is False
    with pytest.raises(ck.CheckpointError, match="still open"):
        app.finish()


def test_scan_images_are_jpegs_and_unknown_sheets_fail(month):
    app = web.ReviewApp(month)
    img = app.scan_image("scan.pdf#2")
    assert img[:3] == b"\xff\xd8\xff" and app.scan_image("scan.pdf#2") is img        # cached
    with pytest.raises(KeyError):
        app.scan_image("scan.pdf#99")


# ------------------------------------------------------------------ over HTTP
@pytest.fixture
def server(month):
    srv, token = web.make_server(web.ReviewApp(month))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", token
    srv.shutdown()
    srv.server_close()


def fetch(url, data=None, headers=None):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_page_is_served_with_the_token_and_safe_headers(server):
    base, token = server
    status, body, headers = fetch(base + "/")
    assert status == 200 and token.encode() in body and b"__TOKEN__" not in body
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"


def test_api_needs_the_token_and_a_local_host(server):
    base, token = server
    assert fetch(base + "/api/state")[0] == 403
    assert fetch(base + "/api/state?t=wrong")[0] == 403
    assert fetch(base + "/api/decision", {"key": "scan.pdf#3", "decision": {"skip": "x"}})[0] == 403
    assert fetch(base + "/api/state?t=" + token, headers={"Host": "evil.example.com"})[0] == 403
    status, body, _ = fetch(base + "/api/state?t=" + token)
    assert status == 200 and json.loads(body)["month"] == "October 2026"


def test_decision_and_finish_over_http(server):
    base, token = server
    h = {"X-Token": token, "Content-Type": "application/json"}
    status, body, _ = fetch(base + "/api/decision", {"key": "scan.pdf#3", "decision": {"skip": "blank"}}, h)
    assert status == 200 and json.loads(body)["counts"]["skipped"] == 1
    status, body, _ = fetch(base + "/api/decision", {"key": "nope", "decision": {}}, h)
    assert status == 409 and "no sheet" in json.loads(body)["error"]
    status, body, _ = fetch(base + "/api/finish", {}, h)
    assert status == 409 and "still open" in json.loads(body)["error"]
    status, body, headers = fetch(base + "/scan?key=scan.pdf%231&t=" + token)
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body[:2] == b"\xff\xd8"
    assert fetch(base + "/scan?key=zzz&t=" + token)[0] == 404


def test_handwritten_text_is_never_put_into_the_page_as_html():
    html = Path(web.PAGE).read_text(encoding="utf-8")
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html


def test_accept_all_takes_every_complete_suggestion_and_leaves_the_rest(month):
    st = web.ReviewApp(month).accept_all()
    sheets = by_key(st)
    assert sheets["scan.pdf#2"]["status"] == "decided" and sheets["scan.pdf#3"]["status"] == "open"
    assert json.loads(month.decisions_path.read_text()) == {
        "scan.pdf#2": {"student": "S0002", "remember_name": True, "pages": 600}}
    assert st["acceptable"] == 0 and st["ready"] is False


def test_an_auto_settled_sheet_can_still_be_changed(month):
    app = web.ReviewApp(month)
    st = app.decide("scan.pdf#1", {"student": "S0001", "pages": 35, "teacher": "Tracy"})
    assert by_key(st)["scan.pdf#1"]["status"] == "decided" and by_key(st)["scan.pdf#1"]["form"]["teacher"] == "Tracy"


def test_a_student_with_no_teacher_gets_the_sheets_teacher_pre_filled(month):
    cp = ck.load(month.folder.root / "2026-27" / "start" / "checkpoint.xlsx")
    cp.students["S0002"] = ck.replace(cp.students["S0002"], teacher="")
    ck.add_student(cp, "Other Kid", "Tracy")
    ck.save(cp, month.folder.root / "2026-27" / "start" / "checkpoint.xlsx")
    app = web.ReviewApp(month)
    app.decide("scan.pdf#2", {"student": "S0002", "remember_name": True})      # chosen earlier, teacher not yet
    form = by_key(app.state())["scan.pdf#2"]["form"]
    assert form["student"] == "S0002" and form["teacher"] == "Tracy"


def test_snippets_of_the_scan_are_cut_where_each_value_was_read(month):
    recs = rv.load_records(month.records_path)
    recs[0] = rv.SheetRecord(**{**recs[0].__dict__, "boxes": {"name": [100, 100, 400, 140]}})
    rv.save_records(recs, month.records_path)
    app = web.ReviewApp(month)
    assert by_key(app.state())["scan.pdf#1"]["read"]["boxes"] == ["name"]
    img = app.scan_crop("scan.pdf#1", "name")
    assert img[:3] == b"\xff\xd8\xff"
    pix = pymupdf.Pixmap(img)
    assert pix.width < 612 * 150 / 72 / 1.5 and pix.height < pix.width       # a strip, not the whole page
    with pytest.raises(KeyError):
        app.scan_crop("scan.pdf#1", "total")
    assert rv.load_records(month.records_path)[0].boxes == {"name": [100, 100, 400, 140]}


def test_the_table_view_is_served_beside_the_card_view(server):
    base, token = server
    assert b"review (table)" in fetch(base + "/?view=table")[1]
    assert b"review (table)" not in fetch(base + "/")[1]
    html = Path(web.PAGE_TABLE).read_text(encoding="utf-8")
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html
