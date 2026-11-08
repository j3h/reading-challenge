"""Folders on disk: seasons, months, previous checkpoint, last year's hints, safe writes."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rc import checkpoint as ck  # noqa: E402
from rc import workspace as ws  # noqa: E402


def finish(root, season, month):
    d = Path(root) / season / month
    d.mkdir(parents=True)
    ck.save(ck.Checkpoint(month=month), d / "checkpoint.xlsx")
    return d / "checkpoint.xlsx"


def test_season_runs_july_to_june():
    assert ws.season_for("2026-10") == "2026-27"
    assert ws.season_for("2027-03") == "2026-27"
    assert ws.season_for("2026-07") == "2026-27"
    assert ws.season_for("2026-06") == "2025-26"
    assert ws.season_for("October 2026") == "2026-27"
    assert ws.season_for("2099-12") == "2099-00"


def test_month_folder_paths_and_create(tmp_path):
    mf = ws.Workspace(tmp_path).month_folder("October 2026").create()
    assert mf.path == tmp_path / "2026-27" / "2026-10"
    assert mf.scans.is_dir() and mf.work.is_dir()
    assert mf.checkpoint == mf.path / "checkpoint.xlsx" and not mf.is_finished()


def test_scan_files_lists_only_scans(tmp_path):
    mf = ws.Workspace(tmp_path).month_folder("2026-10").create()
    for name in ("b.pdf", "a.JPG", "notes.txt", ".hidden.pdf"):
        (mf.scans / name).write_bytes(b"x")
    assert [p.name for p in mf.scan_files()] == ["a.JPG", "b.pdf"]


def test_missing_data_folder_is_a_clear_error(tmp_path):
    with pytest.raises(ck.CheckpointError, match="does not exist"):
        ws.Workspace(tmp_path / "nope")


def test_previous_checkpoint_is_the_latest_earlier_month(tmp_path):
    finish(tmp_path, "2026-27", "2026-10")
    nov = finish(tmp_path, "2026-27", "2026-11")
    path, why = ws.Workspace(tmp_path).previous_checkpoint("2026-12")
    assert path == nov and "November 2026" in why


def test_first_month_uses_the_start_checkpoint(tmp_path):
    start = tmp_path / "2026-27" / "start"
    start.mkdir(parents=True)
    ck.save(ck.Checkpoint(), start / "checkpoint.xlsx")
    path, why = ws.Workspace(tmp_path).previous_checkpoint("2026-10")
    assert path == start / "checkpoint.xlsx" and "starting checkpoint" in why


def test_new_year_starts_with_no_checkpoint_even_if_last_year_exists(tmp_path):
    finish(tmp_path, "2025-26", "2026-05")
    path, why = ws.Workspace(tmp_path).previous_checkpoint("2026-10")
    assert path is None and "first month" in why


def test_rerunning_a_finished_or_older_month_is_refused(tmp_path):
    finish(tmp_path, "2026-27", "2026-11")
    for month in ("2026-11", "2026-10"):
        with pytest.raises(ck.CheckpointError, match="not newer"):
            ws.Workspace(tmp_path).previous_checkpoint(month)


def test_hints_come_from_the_end_of_the_previous_season(tmp_path):
    finish(tmp_path, "2025-26", "2026-04")
    last = finish(tmp_path, "2025-26", "2026-05")
    finish(tmp_path, "2026-27", "2026-10")                  # current season is never its own hints
    assert ws.Workspace(tmp_path).hints_checkpoint("2026-11") == last


def test_hints_fall_back_to_a_start_checkpoint_and_to_none(tmp_path):
    w = ws.Workspace(tmp_path)
    assert w.hints_checkpoint("2026-10") is None
    start = tmp_path / "2025-26" / "start"
    start.mkdir(parents=True)
    ck.save(ck.Checkpoint(), start / "checkpoint.xlsx")
    assert w.hints_checkpoint("2026-10") == start / "checkpoint.xlsx"


def test_write_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "sub" / "out.txt"
    ws.write_atomic(target, "hello")
    ws.write_atomic(target, "hello again")
    assert target.read_text() == "hello again"
    assert [p.name for p in target.parent.iterdir()] == ["out.txt"]


def test_failed_save_keeps_the_old_file(tmp_path):
    target = tmp_path / "checkpoint.xlsx"
    target.write_text("old")

    def boom(tmp):
        Path(tmp).write_text("half")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        ws.save_atomic(boom, target)
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["checkpoint.xlsx"]


def test_save_atomic_with_a_real_checkpoint(tmp_path):
    target = tmp_path / "m" / "checkpoint.xlsx"
    cp = ck.Checkpoint()
    ck.add_student(cp, "Ada Example", "Lopez")
    ws.save_atomic(lambda tmp: ck.save(cp, tmp), target)
    assert ck.load(target).students == cp.students


def test_icloud_placeholders_are_found(tmp_path):
    (tmp_path / "2026-27").mkdir()
    (tmp_path / "2026-27" / ".checkpoint.xlsx.icloud").write_bytes(b"")
    assert [p.name for p in ws.cloud_placeholders(tmp_path)] == [".checkpoint.xlsx.icloud"]
