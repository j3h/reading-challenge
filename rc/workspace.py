"""Where everything lives on disk.

The data folder is outside the git repo (it holds children's names), typically in iCloud Drive so
Jamie can see it:

    <data folder>/
      2026-27/                        one folder per school year ("season")
        start/checkpoint.xlsx         optional: the very first checkpoint (e.g. imported master sheet)
        2026-10/                      one folder per month
          scans/                      the scanned sheets for that month (the original PDF)
          work/                       extraction and review decisions (resumable)
          checkpoint.xlsx             the result: input for next month
          certificates.pdf            the print file, by classroom
          summary.txt                 what changed, new students, anything flagged
        2026-11/ ...
      2027-28/                        a new year starts with no checkpoint

A month's input checkpoint is the latest earlier month in the same season, else `start/`, else
nothing (a fresh year). Last year's final checkpoint is offered separately as *hints* for matching
names and teachers; its totals are never carried in.

Files are written to a temporary name and renamed, so a sync client never uploads half a file.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rc import checkpoint as ck

SEASON_START_MONTH = 7   # July: a season runs July..June
CHECKPOINT = "checkpoint.xlsx"
CERTIFICATES = "certificates.pdf"
SUMMARY = "summary.txt"


def season_for(month: str, start_month: int = SEASON_START_MONTH) -> str:
    """'2026-10' -> '2026-27'; '2027-03' -> '2026-27'."""
    year, mon = (int(p) for p in ck.month_key(month).split("-"))
    first = year if mon >= start_month else year - 1
    return f"{first}-{(first + 1) % 100:02d}"


def _season_first_year(season: str) -> int:
    return int(season.split("-")[0])


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
        exts = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
        return sorted(p for p in self.scans.glob("*") if p.suffix.lower() in exts and not p.name.startswith("."))


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
        """(path, description) of the checkpoint to start `month` from, or (None, why not)."""
        mf = self.month_folder(month, season)
        earlier = [m for m in self.finished_months(mf.season) if m < mf.month]
        if earlier:
            return self.root / mf.season / earlier[-1] / CHECKPOINT, f"checkpoint from {ck.month_label(earlier[-1])}"
        later = [m for m in self.finished_months(mf.season) if m >= mf.month]
        if later:
            raise ck.CheckpointError(
                f"{ck.month_label(mf.month)} is not newer than the already finished {ck.month_label(later[0])} "
                f"in {mf.season}; use a later month, or move that folder aside to redo it")
        start = self.start_checkpoint(mf.season)
        if start:
            return start, f"starting checkpoint for {mf.season}"
        return None, f"no earlier checkpoint in {mf.season}: this is the first month"

    def hints_checkpoint(self, month: str, season: str | None = None) -> Path | None:
        """Last finished checkpoint of the most recent earlier season. For matching hints only."""
        mf = self.month_folder(month, season)
        for s in reversed([s for s in self.seasons() if _season_first_year(s) < _season_first_year(mf.season)]):
            months = self.finished_months(s)
            if months:
                return self.root / s / months[-1] / CHECKPOINT
            start = self.start_checkpoint(s)
            if start:
                return start
        return None


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
