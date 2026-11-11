"""The app's home screen logic and its routes: months, adding scans, reading, reopening. Fake children only."""
import io
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from rc import app as app_mod  # noqa: E402
from rc import checkpoint as ck  # noqa: E402
from rc import review as rv  # noqa: E402
from rc import review_web as web  # noqa: E402
from rc import settings  # noqa: E402
from test_extract import FakeClient, answer  # noqa: E402


class FakeKeyring:
    """Stands in for the Mac Keychain: tests never touch the real one."""
    def __init__(self):
        self.saved = {}

    def get_password(self, service, account):
        return self.saved.get((service, account))

    def set_password(self, service, account, key):
        self.saved[(service, account)] = key

    def delete_password(self, service, account):
        self.saved.pop((service, account), None)


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_keyring", lambda ring=FakeKeyring(): ring)
    monkeypatch.setenv("READING_CHALLENGE_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("READING_CHALLENGE_DATA", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez", "2", total=74)
    ck.add_student(cp, "Io Li", "Park", "3", total=2400)
    (root / "2026-27" / "start").mkdir(parents=True)
    ck.save(cp, root / "2026-27" / "start" / "checkpoint.xlsx")
    return root


def pdf_bytes(pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=612, height=792).insert_text((72, 100), f"sheet {i + 1}", fontsize=20)
    return doc.tobytes()


def wait_for(home):
    for _ in range(200):
        if not home.job.running:
            return
        time.sleep(0.02)
    raise AssertionError("reading did not finish")


def test_without_a_data_folder_the_home_screen_asks_for_one(tmp_path):
    st = app_mod.Home().state()
    assert st["folder_ok"] is False and st["months"] == [] and st["api_key"] == ""
    with pytest.raises(ck.CheckpointError, match="choose the data folder"):
        app_mod.Home().create_month("2026-10")


def test_the_chosen_folder_is_remembered(data):
    app_mod.Home().set_data_folder(str(data))
    assert app_mod.Home().state()["folder_ok"] and json.loads(settings.settings_path().read_text())["data_folder"] == str(data)
    with pytest.raises(ck.CheckpointError, match="not a folder"):
        app_mod.Home().set_data_folder(str(data / "nope"))


def test_a_month_from_scans_to_finished_and_reopened(data):
    home = app_mod.Home(data)
    st = home.create_month("October 2026")
    oct_ = st["months"][0]
    assert oct_["key"] == "2026-10" and oct_["step"] == "scans" and "starting checkpoint" in oct_["starts_from"]
    assert st["suggested"] == ""            # October is not finished yet

    body = pdf_bytes(2)
    st = home.add_scan("2026-10", "oct.pdf", io.BytesIO(body), len(body))
    oct_ = st["months"][0]
    assert oct_["pages"] == 2 and oct_["step"] == "read" and oct_["estimate"] == 0.07

    client = FakeClient([answer("Ada Example", "Lopez", total=30), answer("Io Li", "Park", total=150)])
    home.start_reading("2026-10", client=client)
    wait_for(home)
    assert home.job.done == 2 and home.job.sent == 2 and not home.job.error and "Read 2 sheet(s)" in home.job.result
    oct_ = home.state()["months"][0]
    assert oct_["read"] == 2 and oct_["step"] == "finish" and oct_["open"] == 0      # both exact matches

    home.review("2026-10").finish()
    oct_ = home.state()["months"][0]
    assert oct_["finished"] and oct_["step"] == "finished"
    with pytest.raises(ck.CheckpointError, match="reopen it"):
        home.add_scan("2026-10", "more.pdf", io.BytesIO(body), len(body))

    st = home.reopen("2026-10")
    assert st["months"][0]["step"] == "finish"                     # decisions and readings kept
    aside = [p for p in (data / "2026-27" / "2026-10").iterdir() if p.name.startswith("reopened ")]
    assert len(aside) == 1 and (aside[0] / "checkpoint.xlsx").exists() and (aside[0] / "certificates.pdf").exists()


def test_reopening_is_refused_when_a_later_month_built_on_it(data):
    home = app_mod.Home(data)
    for month, total in (("2026-10", 30), ("2026-11", 20)):
        home.create_month(month)
        body = pdf_bytes(1)
        home.add_scan(month, "s.pdf", io.BytesIO(body), len(body))
        home.start_reading(month, client=FakeClient([answer("Ada Example", "Lopez", total=total)]))
        wait_for(home)
        home.review(month).finish()
    with pytest.raises(ck.CheckpointError, match="reopen that one first"):
        home.reopen("2026-10")
    home.reopen("2026-11")
    home.reopen("2026-10")


def test_bad_uploads_are_refused_and_leave_nothing_behind(data):
    home = app_mod.Home(data)
    home.create_month("2026-10")
    for name, body in (("notes.txt", b"hi"), (".hidden.pdf", b"x"), ("../evil.pdf", b"%PDF")):
        with pytest.raises(ck.CheckpointError):
            home.add_scan("2026-10", name, io.BytesIO(body), len(body))
    with pytest.raises(ck.CheckpointError, match="not a PDF"):
        home.add_scan("2026-10", "junk.pdf", io.BytesIO(b"not a pdf at all"), 16)
    with pytest.raises(ck.CheckpointError, match="cut short"):
        home.add_scan("2026-10", "short.pdf", io.BytesIO(b"abc"), 100)
    assert list((data / "2026-27" / "2026-10" / "scans").iterdir()) == []
    assert not (data / "evil.pdf").exists()


def test_reading_needs_scans_and_a_key(data):
    home = app_mod.Home(data)
    home.create_month("2026-10")
    with pytest.raises(ck.CheckpointError, match="add the scans first"):
        home.start_reading("2026-10", client=FakeClient([answer()]))
    body = pdf_bytes(1)
    home.add_scan("2026-10", "s.pdf", io.BytesIO(body), len(body))
    with pytest.raises(ck.CheckpointError, match="no Claude API key"):
        home.start_reading("2026-10")


def test_a_failed_page_is_reported_in_words(data):
    home = app_mod.Home(data)
    home.create_month("2026-10")
    body = pdf_bytes(2)
    home.add_scan("2026-10", "s.pdf", io.BytesIO(body), len(body))
    home.start_reading("2026-10", client=FakeClient([answer()], fail_on={0, 1, 2, 3, 4, 5, 6, 7, 8, 9}))
    wait_for(home)
    assert "could not be read" in home.job.error and home.job.failed == 2


def test_api_keys_are_checked_before_saving(monkeypatch):
    with pytest.raises(ValueError, match="does not look like"):
        settings.set_api_key("hello")
    settings.set_api_key("  sk-ant-" + "x" * 40 + "\n")
    assert settings.api_key() == "sk-ant-" + "x" * 40 and settings.api_key_source() == "keychain"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert settings.api_key() == "sk-ant-from-env" and settings.api_key_source() == "environment"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    settings.forget_api_key()
    assert settings.api_key_source() == ""


# ------------------------------------------------------------------ over HTTP
@pytest.fixture
def server(data):
    home = app_mod.Home(data)
    srv, token = web.make_server(home=home)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", token, home
    srv.shutdown()
    srv.server_close()


def call(url, body=None, raw=None, token=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, headers={"X-Token": token or ""}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_home_routes(server):
    base, token, home = server
    status, page = call(base + "/")
    assert status == 200 and b"Start a month" in page and token.encode() in page
    assert call(base + "/ping") == (200, b"rc-app")
    assert call(base + "/api/home")[0] == 403
    status, body = call(base + "/api/home/month", {"month": "2026-10"}, token=token)
    assert status == 200 and json.loads(body)["months"][0]["key"] == "2026-10"
    pdf = pdf_bytes(1)
    status, body = call(f"{base}/api/home/upload?m=2026-10&name=s.pdf", raw=pdf, token=token)
    assert status == 200 and json.loads(body)["months"][0]["pages"] == 1
    status, body = call(f"{base}/api/home/upload?m=2026-10&name=s.pdf", raw=pdf, token=token)
    assert status == 409 and b"already a scan" in body
    assert call(f"{base}/api/home/upload?m=2026-10&name=x.pdf", raw=pdf)[0] == 403        # no token
    status, page = call(base + "/review?m=2026-10")
    assert status == 200 and b"All months" in page
    rv.save_records([rv.SheetRecord("s.pdf", 1, "Ada Example", "Lopez", "October", 30)],
                    home.month("2026-10").records_path)
    status, body = call(f"{base}/api/state?m=2026-10&t={token}")
    assert status == 200 and json.loads(body)["month"] == "October 2026"
    assert call(base + "/api/home/nonsense", {}, token=token)[0] == 409


def test_a_new_month_waits_until_earlier_months_are_finished(data):
    home = app_mod.Home(data)
    st = home.create_month("2026-10")
    assert st["suggested"] == "" and st["unfinished"] == ["October 2026"]
    with pytest.raises(ck.CheckpointError, match="finish October 2026 first"):
        home.create_month("2026-11")


def test_students_page_lists_shows_history_and_records_corrections(tmp_path, monkeypatch):
    from rc import events as ev
    data = tmp_path / "evdata"
    data.mkdir()
    log = ev.EventLog(data, "mac")
    src = ({"workbook": "m.xlsx", "tab": "Sept", "row": 7},)
    log.append([ev.Event("student_added", {"student": "S0001", "name": "Ada Example", "teacher": "Lopez", "grade": "2"},
                         "Josh", "", src, "2026-09-28T12:00:00+00:00"),
                ev.Event("pages_recorded", {"student": "S0001", "month": "2026-09", "pages": 120}, "Josh", "", src,
                         "2026-09-28T12:00:01+00:00")])
    home = app_mod.Home(data)
    people = home.students()
    assert people["students"][0]["name"] == "Ada Example" and people["students"][0]["owed"] == ["Bookmark", "Pencil and Stickers"]
    with pytest.raises(ck.CheckpointError, match="name in Settings"):
        home.adjust("S0001", {"teacher": "Park", "reason": "moved"})
    settings.save({"who": "Jamie"})
    with pytest.raises(ck.CheckpointError, match="say why"):
        home.adjust("S0001", {"teacher": "Park"})
    got = home.adjust("S0001", {"teacher": "Park", "grade": "2", "name": "Ada Example", "pages": -20, "month": "2026-09",
                                "reason": "a book was counted twice"})
    assert got["student"]["teacher"] == "Park" and got["student"]["total"] == 100
    newest = got["timeline"][0]
    assert newest["who"] == "Jamie" and newest["sources"] == ["by hand"] and newest["reason"] == "a book was counted twice"
    assert [e["text"].split(" - ")[0] for e in got["timeline"]][:2] == ["September 2026: pages -20", "teacher set to Park"]
    first = got["timeline"][-1]
    assert first["sources"] == ["m.xlsx: Sept row 7"]
    wrong = next(e for e in got["timeline"] if e["kind"] == "pages_adjusted")
    got = home.adjust("S0001", {"void": wrong["id"], "reason": "it was not counted twice after all"})
    assert got["student"]["total"] == 120
    crossed = next(e for e in got["timeline"] if e["id"] == wrong["id"])
    assert crossed["cancelled"] and crossed["cancelled_by"] == "Jamie" and "after all" in crossed["cancel_reason"]
    with pytest.raises(ck.CheckpointError, match="nothing to change"):
        home.adjust("S0001", {"teacher": "Park", "reason": "same as before"})


def test_old_months_scans_can_be_let_go_and_nothing_else(data, monkeypatch):
    home = app_mod.Home(data)
    trashed = []
    monkeypatch.setattr(home, "_trash", lambda files: [trashed.append(p) or p.unlink() for p in files])
    settings.save({"who": "Jamie", "keep_raw_months": 2})
    for month in ("2026-10", "2026-11", "2026-12", "2027-01"):
        home.create_month(month)
        body = pdf_bytes(1)
        home.add_scan(month, "s.pdf", io.BytesIO(body), len(body))
        home.start_reading(month, client=FakeClient([answer("Ada Example", "Lopez", total=10)]))
        wait_for(home)
        home.review(month).finish()
    st = home.state()
    assert [p["key"] for p in st["prunable"]] == ["2026-10"]          # more than 2 months before January
    with pytest.raises(ck.CheckpointError, match="not finished long enough ago"):
        home.let_go(["2026-11"])
    st = home.let_go(["2026-10"])
    oct_ = data / "2026-27" / "2026-10"
    assert {p.name for p in trashed} == {"s.pdf", "s.p0001.json"} and st["prunable"] == []
    assert (oct_ / "checkpoint.xlsx").exists() and (oct_ / "certificates.pdf").exists() and (oct_ / "work" / "records.json").exists()
    note = (oct_ / "work" / "removed.txt").read_text()
    assert "Jamie moved 2 raw file(s) to the Trash" in note and "scans/s.pdf" in note
    assert home.month_status("2026-10")["finished"]                    # still a finished month
    with pytest.raises(ck.CheckpointError, match="at least 1"):
        home.set_keep_raw(0)
