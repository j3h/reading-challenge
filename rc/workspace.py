"""Where everything lives on disk.

The data folder is outside the git repo (it holds children's names), typically in iCloud Drive so
Jamie can see it:

With an event log (the usual case now) sheets are not filed by month until they are printed:

    <data folder>/
      Scans/                          every scan, photo and form download, as it arrived; never moved
      Inbox.txt                       what is still waiting, written by the app for people
      App working files (do not edit)/  readings, decisions, file fingerprints
      2026-27/2026-10 round 1/        what one round printed: certificates.pdf, summary.txt, checkpoint.xlsx
      2026-27/2026-10 round 2/        a later round for the same month (late sheets)

Older data folders (and tests) use one folder per month:

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
CLASS_LISTS = "class-lists.pdf"
SUMMARY = "summary.txt"
SCAN_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
PHOTO_EXTS = {".webp", ".heic", ".heif"}        # emailed or phone photos: converted to JPEG on the way in
FORM_EXTS = {".csv"}                             # Google Form responses, downloaded as CSV (rc/forms.py)


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
    def class_lists(self) -> Path:
        return self.path / CLASS_LISTS

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

    def form_files(self) -> list[Path]:
        return sorted(p for p in self.scans.glob("*") if p.suffix.lower() in FORM_EXTS and not p.name.startswith("."))

    def convert_photos(self) -> list[str]:
        """Turn .webp/.heic photos in scans/ into JPEGs the rest of the program can read (macOS `sips`).
        The original moves to scans/originals/. Returns the new file names."""
        return convert_photos(self.scans)

    def registry(self) -> "FileRegistry":
        return FileRegistry(self.scans, self.work)


SCANS = "Scans"
WORK = "App working files (do not edit)"
INBOX_LIST = "Inbox.txt"


@dataclass(frozen=True)
class Inbox(MonthFolder):
    """Every scan, photo and form download, kept where it arrived for good (Scans/), and what was read from them
    and decided about them (the app's working folder). Which sheets are still waiting is worked out from the event
    log and written to Inbox.txt for people; nothing ever moves. A sheet's month is chosen when a round prints it."""
    month: str = "inbox"
    season: str = ""

    @property
    def path(self) -> Path:
        return self.root

    @property
    def scans(self) -> Path:
        return self.root / SCANS

    @property
    def work(self) -> Path:
        return self.root / WORK

    @property
    def inbox_list(self) -> Path:
        return self.root / INBOX_LIST

    def is_finished(self) -> bool:
        return False


@dataclass(frozen=True)
class RoundFolder:
    """One round's outputs: <school year>/<YYYY-MM> round <n>/ (certificates.pdf, summary.txt, checkpoint.xlsx)."""
    root: Path
    month: str
    number: int

    @property
    def path(self) -> Path:
        return self.root / season_for(self.month) / f"{self.month} round {self.number}"

    @property
    def certificates(self) -> Path:
        return self.path / CERTIFICATES

    @property
    def summary(self) -> Path:
        return self.path / SUMMARY

    @property
    def checkpoint(self) -> Path:
        return self.path / CHECKPOINT

    @property
    def class_lists(self) -> Path:
        return self.path / CLASS_LISTS

    def create(self) -> "RoundFolder":
        self.path.mkdir(parents=True, exist_ok=True)
        return self


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser()
        if not self.root.is_dir():
            raise ck.CheckpointError(f"data folder {self.root} does not exist")

    def month_folder(self, month: str, season: str | None = None) -> MonthFolder:
        key = ck.month_key(month)
        return MonthFolder(self.root, key, season or season_for(key))

    def inbox(self) -> Inbox:
        return Inbox(self.root)

    def round_folder(self, month: str, number: int) -> RoundFolder:
        return RoundFolder(self.root, ck.month_key(month), number)

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


def file_sha256(path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FileRegistry:
    """The SHA-256 of every file in a scans folder (originals/ included), as first seen: work/files.json.

    It is the fingerprint events point at, so a file can be checked later against what was read; it also catches the
    same file added twice under another name, and a file swapped for a different one under the same name. Hashing is
    skipped while a file's size and modified time are unchanged."""

    def __init__(self, scans, work):
        self.scans, self.path = Path(scans), Path(work) / "files.json"

    def load(self) -> dict:
        import json
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except ValueError:
            return {}

    def _save(self, data: dict) -> None:
        import json
        write_atomic(self.path, json.dumps(data, indent=1, sort_keys=True))

    def files(self) -> list[Path]:
        if not self.scans.is_dir():
            return []
        return sorted(p for p in self.scans.rglob("*") if p.is_file() and not p.name.startswith(".")
                      and (p.suffix.lower() in SCAN_EXTS | PHOTO_EXTS | FORM_EXTS))

    def name(self, path: Path) -> str:
        return path.relative_to(self.scans).as_posix()

    def check(self) -> tuple[dict, list[str]]:
        """({name: sha256} for every file now, [problems]). New files are registered. A scan or photo whose content
        changed since it was registered is a problem (a form download is expected to change: it is re-registered)."""
        data, changed, problems, now = self.load(), False, [], {}
        for p in self.files():
            name, st = self.name(p), p.stat()
            old = data.get(name)
            if old and old.get("size") == st.st_size and old.get("mtime") == st.st_mtime:
                now[name] = old["sha256"]
                continue
            digest = file_sha256(p)
            now[name] = digest
            if old and old["sha256"] != digest and p.suffix.lower() not in FORM_EXTS:
                problems.append(f"{name} is not the same file it was when it was added (its contents changed); put the "
                                f"original back, or add the new one under another name")
                continue                        # keep the original fingerprint until someone sorts it out
            data[name] = {"sha256": digest, "size": st.st_size, "mtime": st.st_mtime,
                          **({"first": old.get("first")} if old and old.get("first") else {})}
            changed = True
        if changed:
            self._save(data)
        groups: dict = {}
        for name, digest in now.items():                 # e.g. the same scan dropped in twice under two names
            groups.setdefault(digest, []).append(name)
        for names in groups.values():
            if len(names) > 1:
                problems.append(f"{' and '.join(sorted(names))} are the same file; move one of them out of the "
                                f"Scans folder (its sheets would count twice)")
        return now, problems

    def find(self, digest: str, exclude: str = "") -> str:
        """The name of a file already here with this SHA-256, or ''."""
        now, _ = self.check()
        return next((n for n, d in now.items() if d == digest and n != exclude), "")

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


def _unhide(path: Path) -> None:
    """The temporary file has a dot name; in a synced folder macOS can mark it hidden (UF_HIDDEN), and the flag
    survives the rename, so Finder would not show the finished file. Clear it."""
    import stat
    try:
        flags = os.stat(path).st_flags
        if flags & stat.UF_HIDDEN:
            os.chflags(path, flags & ~stat.UF_HIDDEN)
    except (AttributeError, OSError):          # no file flags on this system: nothing to clear
        pass


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
        _unhide(path)
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
        _unhide(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cloud_placeholders(folder) -> list[Path]:
    """iCloud files that are not downloaded yet show up as '.name.icloud'. Reading them would fail."""
    return sorted(Path(folder).rglob(".*.icloud"))
