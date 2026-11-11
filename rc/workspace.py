"""Where everything lives on disk.

The data folder is outside the git repo (it holds children's names), typically in iCloud Drive so
Jamie can see it:

    <data folder>/
      2026-27/                        one folder per school year, only to keep months tidy
        start/checkpoint.xlsx         optional: the very first checkpoint (e.g. imported master sheet)
        2026-10/                      one folder per month
          scans/                      the scanned sheets for that month (the original PDF)
          work/                       extraction and review decisions (resumable)
          checkpoint.xlsx             the result: input for next month
          certificates.pdf            the print file, by classroom
          summary.txt                 what changed, new students, anything flagged
        2026-11/ ...
      2027-28/
        2027-09/                      starts from 2027-28's last checkpoint: totals keep rolling forward

Totals never reset: a month's input checkpoint is the latest earlier finished month in ANY school year,
else the most recent `start/` checkpoint, else nothing (the very first month).

Files are written to a temporary name and renamed, so a sync client never uploads half a file.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rc import checkpoint as ck

SEASON_START_MONTH = 7   # July: a school-year folder runs July..June
CHECKPOINT = "checkpoint.xlsx"
CERTIFICATES = "certificates.pdf"
SUMMARY = "summary.txt"
SCAN_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
PHOTO_EXTS = {".webp", ".heic", ".heif"}        # emailed or phone photos: converted to JPEG on the way in


def season_for(month: str, start_month: int = SEASON_START_MONTH) -> str:
    """'2026-10' -> '2026-27'; '2027-03' -> '2026-27'."""
    year, mon = (int(p) for p in ck.month_key(month).split("-"))
    first = year if mon >= start_month else year - 1
    return f"{first}-{(first + 1) % 100:02d}"


@dataclass(frozen=True)
class MonthFolder:
    root: Path
    month: str            # '2026-10'
    season: str           # '2026-27'

    @property
    def path(self) -> Path:
        return self.root / self.season / self.month

    @property
    def scans(self) -> Path:
        return self.path / "scans"

    @property
    def work(self) -> Path:
        return self.path / "work"

    @property
    def checkpoint(self) -> Path:
        return self.path / CHECKPOINT

    @property
    def certificates(self) -> Path:
        return self.path / CERTIFICATES

    @property
    def summary(self) -> Path:
        return self.path / SUMMARY

    def create(self) -> "MonthFolder":
        for d in (self.scans, self.work):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def is_finished(self) -> bool:
        return self.checkpoint.exists()

    def scan_files(self) -> list[Path]:
        return sorted(p for p in self.scans.glob("*") if p.suffix.lower() in SCAN_EXTS and not p.name.startswith("."))

    def convert_photos(self) -> list[str]:
        """Turn .webp/.heic photos in scans/ into JPEGs the rest of the program can read (macOS `sips`).
        The original moves to scans/originals/. Returns the new file names."""
        return convert_photos(self.scans)


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise ck.CheckpointError(f"data folder {self.root} does not exist")

    def month_folder(self, month: str, season: str | None = None) -> MonthFolder:
        key = ck.month_key(month)
        return MonthFolder(self.root, key, season or season_for(key))

    def seasons(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}", p.name))

    def finished_months(self, season: str) -> list[str]:
        base = self.root / season
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir()
                      if re.fullmatch(r"\d{4}-\d{2}", p.name) and (p / CHECKPOINT).exists())

    def start_checkpoint(self, season: str) -> Path | None:
        p = self.root / season / "start" / CHECKPOINT
        return p if p.exists() else None

    def previous_checkpoint(self, month: str, season: str | None = None) -> tuple[Path | None, str]:
        """(path, description) of the checkpoint to start `month` from, or (None, why not).

        Looks across every school-year folder: totals roll forward from year to year."""
        mf = self.month_folder(month, season)
        finished = sorted((m, s) for s in self.seasons() for m in self.finished_months(s))
        later = [(m, s) for m, s in finished if m >= mf.month]
        if later:
            m, s = later[0]
            raise ck.CheckpointError(
                f"{ck.month_label(mf.month)} is not newer than the already finished {ck.month_label(m)} "
                f"({s}/{m}); use a later month, or move that folder aside to redo it")
        if finished:
            m, s = finished[-1]
            return self.root / s / m / CHECKPOINT, f"checkpoint from {ck.month_label(m)}"
        starts = [s for s in self.seasons() if s <= mf.season and self.start_checkpoint(s)]
        if starts:
            return self.start_checkpoint(starts[-1]), f"starting checkpoint in {starts[-1]}/start"
        return None, "no earlier checkpoint: this is the first month"


def convert_photos(folder) -> list[str]:
    import shutil
    import subprocess
    import sys
    folder = Path(folder)
    photos = sorted(p for p in folder.glob("*") if p.suffix.lower() in PHOTO_EXTS and not p.name.startswith("."))
    done = []
    for src in photos:
        if sys.platform != "darwin":
            raise ck.CheckpointError(f"{src.name}: convert .webp/.heic photos to JPEG first (only automatic on a Mac)")
        target = folder / (src.stem + ".jpg")
        n = 2
        while target.exists():
            target, n = folder / f"{src.stem} ({n}).jpg", n + 1
        tmp = folder / f".{target.name}.converting.jpg"
        result = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90", str(src), "--out", str(tmp)],
                                capture_output=True, text=True)
        if result.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise ck.CheckpointError(f"could not convert {src.name} to JPEG: {result.stderr.strip() or 'sips failed'}")
        tmp.replace(target)
        (folder / "originals").mkdir(exist_ok=True)
        shutil.move(str(src), folder / "originals" / src.name)
        done.append(target.name)
    return done


def write_atomic(path, data: bytes | str) -> None:
    """Write via a temp file in the same folder, then rename (sync clients see only the finished file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode, kwargs = ("wb", {}) if isinstance(data, bytes) else ("w", {"encoding": "utf-8"})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, **kwargs) as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_atomic(save_fn, path) -> None:
    """For writers that need a filename (openpyxl, PyMuPDF): `save_fn(tmp_path)`, then rename into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=path.suffix)
    os.close(fd)
    try:
        save_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cloud_placeholders(folder) -> list[Path]:
    """iCloud files that are not downloaded yet show up as '.name.icloud'. Reading them would fail."""
    return sorted(Path(folder).rglob(".*.icloud"))
