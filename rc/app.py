"""The whole monthly job without a command line: the logic behind the app's home screen.

    python -m rc app            (or double-click "Reading Challenge" once set up: see tools/setup_mac.sh)

A month goes: add the scans -> read them (Claude) -> review -> finish -> (reopen, if something was wrong).
Each step is a method here; review_web.py puts them on a local web page. Nothing here talks HTTP, so it can be
tested directly.
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from rc import checkpoint as ck
from rc import extract
from rc import review as rv
from rc import runner
from rc import settings
from rc import workspace as wsp

SCAN_EXTS = wsp.SCAN_EXTS | wsp.PHOTO_EXTS | wsp.FORM_EXTS
MAX_UPLOAD = 500 * 1024 * 1024          # a month's scan is ~50 MB
MONTH_DIR = re.compile(r"\d{4}-\d{2}")


@dataclass
class ReadJob:
    month: str
    total: int = 0                # pages to read (all of them; already-read ones pass quickly)
    done: int = 0
    sent: int = 0                 # pages actually sent to Claude (cost)
    failed: int = 0
    running: bool = True
    error: str = ""
    result: str = ""
    started: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("month", "total", "done", "sent", "failed", "running", "error",
                                              "result", "started")}


class Home:
    def __init__(self, root=None):
        self.root = Path(root).expanduser() if root else settings.data_folder()
        self._reviews: dict = {}
        self._pages: dict = {}
        self.job: ReadJob | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ where things are
    @property
    def ws(self) -> wsp.Workspace:
        if not self.root or not self.root.is_dir():
            raise ck.CheckpointError("choose the data folder first (Settings)")
        return wsp.Workspace(self.root)

    def month(self, month: str) -> runner.Month:
        return runner.Month(self.ws, month)

    def review(self, month: str):
        """The review screen's backend for one month (kept, so scan images stay cached)."""
        from rc import review_web
        key = ck.month_key(month)
        if key not in self._reviews:
            self._reviews[key] = review_web.ReviewApp(self.month(key))
        return self._reviews[key]

    def set_data_folder(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise ck.CheckpointError(f"{p} is not a folder")
        settings.save({"data_folder": str(p)})
        self.root, self._reviews = p, {}
        return self.state()

    def set_who(self, name: str) -> dict:
        name = " ".join(str(name).split())
        if not name:
            raise ck.CheckpointError("type your name: it is recorded on every change you make")
        settings.save({"who": name})
        return self.state()

    def choose_data_folder(self) -> dict:
        """Ask with the Mac's own folder picker (a browser page cannot see folder paths)."""
        if sys.platform != "darwin":
            raise ck.CheckpointError("the folder picker only works on a Mac; type the path instead")
        script = 'POSIX path of (choose folder with prompt "Choose the Reading Challenge data folder")'
        done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if done.returncode != 0:
            return self.state()                     # cancelled
        return self.set_data_folder(done.stdout.strip())

    # ------------------------------------------------------------------ what the home screen shows
    def _page_count(self, path: Path) -> int:
        stamp = (path, path.stat().st_mtime, path.stat().st_size)
        if stamp not in self._pages:
            if path.suffix.lower() == ".pdf":
                with pymupdf.open(path) as doc:
                    self._pages[stamp] = doc.page_count
            else:
                self._pages[stamp] = 1
        return self._pages[stamp]

    def month_status(self, key: str) -> dict:
        m = self.month(key)
        f = m.folder
        if f.scans.is_dir() and not f.is_finished():
            f.convert_photos()                      # photos dropped in with Finder
        scans = [{"name": p.name, "pages": self._page_count(p), "mb": round(p.stat().st_size / 1e6, 1)}
                 for p in f.scan_files()]
        pages = sum(s["pages"] for s in scans)
        from rc import forms as forms_mod
        forms = []
        for p in (f.form_files() if f.scans.is_dir() else []):
            try:
                rows, _ = forms_mod.records(p, key)
                forms.append({"name": p.name, "rows": len(rows)})
            except ck.CheckpointError as e:
                forms.append({"name": p.name, "rows": 0, "problem": str(e)})
        raw = f.work / "raw"
        read = sum(1 for p in raw.glob("*.json")) if raw.is_dir() else 0
        out = {"key": key, "label": ck.month_label(key), "folder": str(f.path), "scans": scans, "pages": pages,
               "read": min(read, pages), "records": m.records_path.exists() or bool(forms),
               "finished": f.is_finished(), "forms": forms,
               "open": None, "sheets": None, "problem": "", "starts_from": "",
               "still_in_icloud": len(wsp.cloud_placeholders(f.path)),
               "estimate": round(max(pages - read, 0) * extract.COST_PER_PAGE, 2)}
        try:
            _, out["starts_from"] = m.previous() if not out["finished"] else (None, "")
        except ck.CheckpointError as e:
            out["problem"] = str(e)
        if out["records"] and not out["finished"] and not out["problem"]:
            try:
                p = m.plan()
                out["open"], out["sheets"] = len({i.key for i in p.issues}), len(p.settled)
            except ck.CheckpointError as e:
                out["problem"] = str(e)
        out["step"] = ("finished" if out["finished"] else "scans" if not scans and not forms else
                       "read" if scans and (not m.records_path.exists() or out["read"] < pages) else
                       "review" if out["open"] else "finish")
        return out

    def months(self) -> list[str]:
        root = self.ws.root
        found = set()
        for season in root.iterdir():
            if season.is_dir() and re.fullmatch(r"\d{4}-\d{2}", season.name):
                found |= {p.name for p in season.iterdir() if p.is_dir() and MONTH_DIR.fullmatch(p.name)
                          and wsp.season_for(p.name) == season.name}
        return sorted(found, reverse=True)

    def suggested_month(self) -> str:
        """The month after the latest one there is; else the current month."""
        have = self.months()
        if have:
            y, m = (int(x) for x in have[0].split("-"))
            return f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"
        return dt.date.today().strftime("%Y-%m")

    def state(self) -> dict:
        out = {"data_folder": str(self.root) if self.root else "", "folder_ok": bool(self.root and self.root.is_dir()),
               "who": settings.who(),
               "api_key": settings.api_key_source(), "months": [], "suggested": "", "job": self.job.as_dict()
               if self.job else None, "mac": sys.platform == "darwin"}
        out["keep_raw_months"] = settings.keep_raw_months()
        if out["folder_ok"]:
            out["prunable"] = self.prunable()
            out["months"] = [self.month_status(k) for k in self.months()]
            out["unfinished"] = [m["label"] for m in out["months"] if not m["finished"]]
            out["suggested"] = "" if out["unfinished"] else self.suggested_month()
            try:
                if out["suggested"]:
                    _, out["suggested_starts_from"] = self.ws.previous_checkpoint(out["suggested"])
            except ck.CheckpointError as e:
                out["suggested_starts_from"] = str(e)
        return out

    # ------------------------------------------------------------------ steps
    def create_month(self, month: str) -> dict:
        key = ck.month_key(month)
        self.ws.previous_checkpoint(key)           # refuses a month older than one already finished
        earlier = [k for k in self.months() if k < key and not self.month(k).folder.is_finished()]
        if earlier:                                # it would start from totals that miss that month's pages
            raise ck.CheckpointError(f"finish {ck.month_label(earlier[0])} first: {ck.month_label(key)} starts "
                                     f"from its totals")
        self.month(key).folder.create()
        return self.state()

    def add_scan(self, month: str, name: str, stream, length: int) -> dict:
        """Save an uploaded scan into the month's scans folder (written whole, then renamed into place)."""
        name = Path(name or "").name.strip()
        if not name or name.startswith(".") or Path(name).suffix.lower() not in SCAN_EXTS:
            raise ck.CheckpointError("add PDFs, pictures (.jpg .png .tif .webp .heic) or form responses (.csv)")
        if length <= 0 or length > MAX_UPLOAD:
            raise ck.CheckpointError("that file is empty or too big")
        f = self.month(month).folder.create()
        if f.is_finished():
            raise ck.CheckpointError(f"{ck.month_label(f.month)} is finished; reopen it to add scans")
        target = f.scans / name
        if target.exists():
            raise ck.CheckpointError(f"there is already a scan called {name!r} in this month")
        tmp = f.scans / f".{name}.uploading"
        try:
            with open(tmp, "wb") as out:
                left = length
                while left:
                    chunk = stream.read(min(left, 1 << 20))
                    if not chunk:
                        raise ck.CheckpointError("the upload was cut short; try again")
                    out.write(chunk)
                    left -= len(chunk)
            if target.suffix.lower() in wsp.FORM_EXTS:
                from rc import forms as forms_mod
                forms_mod.records(tmp, f.month)            # refuses a CSV without name and pages columns
            if target.suffix.lower() == ".pdf":
                try:
                    with pymupdf.open(tmp) as doc:
                        if doc.page_count == 0:
                            raise ck.CheckpointError("that PDF has no pages")
                except (RuntimeError, ValueError):
                    raise ck.CheckpointError(f"{name} is not a PDF that can be opened") from None
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        f.convert_photos()
        return self.state()

    def start_reading(self, month: str, client=None) -> dict:
        """Read the month's scans with Claude in the background; the page polls `state()["job"]`."""
        with self._lock:
            if self.job and self.job.running:
                raise ck.CheckpointError(f"already reading {ck.month_label(self.job.month)}")
            m = self.month(month)
            if m.folder.is_finished():
                raise ck.CheckpointError(f"{ck.month_label(m.folder.month)} is finished; reopen it first")
            status = self.month_status(m.folder.month)
            if not status["scans"]:
                raise ck.CheckpointError("add the scans first")
            if status["still_in_icloud"]:
                raise ck.CheckpointError("some files are still downloading from iCloud; open the month's folder in "
                                         "Finder, wait for them, then try again")
            client = client or extract.make_client()
            job = self.job = ReadJob(m.folder.month, total=status["pages"])

        def page_done(ok, sent):
            job.done += 1
            job.sent += bool(sent and ok)
            job.failed += not ok

        def work():
            try:
                records, dropped, usage = extract.extract_month(client, m.folder, log=lambda *_: None,
                                                                on_page=page_done)
                job.result = (f"Read {len(records)} sheet(s)" +
                              (f"; {len(dropped)} page(s) were not reading sheets" if dropped else "") +
                              f". Cost: {extract.cost_estimate(usage, extract.DEFAULT_MODEL)}")
                self._reviews.pop(m.folder.month, None)
            except Exception as e:  # shown on the page in words
                job.error = _plain(e)
            finally:
                job.running = False

        threading.Thread(target=work, daemon=True).start()
        return self.state()

    def reopen(self, month: str, reason: str = "") -> dict:
        """Undo 'finish': cancel the month's events in the log (with who and why), and move its outputs into a
        dated folder beside them. Review decisions are kept."""
        m = self.month(month)
        f = m.folder
        if not f.is_finished():
            raise ck.CheckpointError(f"{ck.month_label(f.month)} is not finished")
        later = [k for k in self.months() if k > f.month and self.month(k).folder.is_finished()]
        if later:
            raise ck.CheckpointError(f"{ck.month_label(later[0])} was finished after this month and started from "
                                     f"its totals; reopen that one first")
        if m.uses_events():
            runner.reopen_month(m, settings.who(), reason)
        aside = f.path / ("reopened " + dt.datetime.now().strftime("%Y-%m-%d %H.%M.%S"))
        aside.mkdir()
        for p in (f.checkpoint, f.certificates, f.class_lists, f.summary):   # checkpoint first: open again
            if p.exists():
                shutil.move(str(p), aside / p.name)
        self._reviews.pop(f.month, None)
        return self.state()

    # ------------------------------------------------------------------ students: history and corrections
    def _log(self):
        from rc import events as ev
        log = ev.EventLog(self.ws.root)
        if not log.has_students():
            raise ck.CheckpointError("this data folder has no event log yet (import the master workbook first)")
        return log

    def students(self) -> dict:
        import make_certificates as mc
        log = self._log()
        st = log.state()
        owed = st.owed(mc.load_levels())
        return {"students": [{"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade, "total": s.total,
                              "left": s.status != ck.ACTIVE, "also": s.nicknames,
                              "owed": [p for _, _, p in owed.get(s.id, [])]}
                             for s in sorted(st.students.values(), key=lambda s: s.name.lower())],
                "problems": log.problems() + st.warnings}

    def student(self, sid: str) -> dict:
        import make_certificates as mc
        from rc import events as ev
        log = self._log()
        st = log.state()
        if sid not in st.students:
            raise KeyError(sid)
        s = st.students[sid]
        by_id = {e.id: e for e in st.events}
        voided_by = {e.data["event"]: e for e in st.events if e.kind == "event_voided"}
        timeline = []
        for e in st.by_student.get(sid, []):
            v = voided_by.get(e.id) if e.id in st.voided else None
            timeline.append({"id": e.id, "time": e.time, "who": e.who, "device": e.device, "kind": e.kind,
                             "text": e.describe(), "reason": e.reason,
                             "sources": [ev.describe_source(x) for x in e.sources],
                             "cancelled": bool(v), "cancelled_by": v.who if v else "",
                             "cancel_reason": v.reason if v else ""})
        return {"student": {"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade, "total": s.total,
                            "notes": s.notes, "also": s.nicknames,
                            "owed": [f"{p} ({t:,})" for t, _, p in st.owed(mc.load_levels()).get(sid, [])]},
                "timeline": timeline[::-1], "teachers": sorted({x.teacher for x in st.students.values() if x.teacher})}

    def adjust(self, sid: str, change: dict) -> dict:
        from rc import events as ev
        log = self._log()
        who = settings.who()
        if not who:
            raise ck.CheckpointError("add your name in Settings first: it is recorded on every change")
        if not str(change.get("reason", "")).strip():
            raise ck.CheckpointError("say why: the reason is kept with the change")
        events = ev.adjustment_events(log.state(), who, change.get("reason", ""), sid,
                                      change.get("teacher"), change.get("grade"), change.get("name"),
                                      change.get("month"), change.get("pages"), change.get("total"),
                                      change.get("note"), change.get("void"))
        log.append(events)
        self._reviews = {}                          # review screens recompute from the new state
        return self.student(sid)

    # ------------------------------------------------------------------ letting raw data go
    def raw_files(self, key: str) -> list[Path]:
        """A month's raw material: scans, photos (and their originals), form CSVs, cached readings."""
        f = self.month(key).folder
        out = []
        for d in (f.scans, f.work / "raw"):
            if d.is_dir():
                out += sorted(p for p in d.rglob("*") if p.is_file() and not p.name.startswith("."))
        return out

    def prunable(self) -> list[dict]:
        """Finished months more than `keep_raw_months` before the latest finished month that still have raw files.
        Their totals, history (event log), certificates, summary, readings (records.json) and review decisions stay."""
        finished = [k for k in self.months() if self.month(k).folder.is_finished()]
        if not finished:
            return []
        y, m = (int(x) for x in max(finished).split("-"))
        back = y * 12 + (m - 1) - settings.keep_raw_months()
        cutoff = f"{back // 12}-{back % 12 + 1:02d}"
        out = []
        for k in sorted(finished):
            if k < cutoff:
                files = self.raw_files(k)
                if files:
                    out.append({"key": k, "label": ck.month_label(k), "files": len(files),
                                "mb": round(sum(p.stat().st_size for p in files) / 1e6, 1)})
        return out

    def let_go(self, months: list[str]) -> dict:
        """Move the raw files of these (prunable) months to the Trash, and note it in each month's folder."""
        allowed = {x["key"] for x in self.prunable()}
        months = [ck.month_key(k) for k in months]
        bad = [k for k in months if k not in allowed]
        if bad:
            raise ck.CheckpointError(f"{ck.month_label(bad[0])} is not finished long enough ago to let its scans go")
        who = settings.who() or "(no name set)"
        for k in months:
            files = self.raw_files(k)
            self._trash(files)
            f = self.month(k).folder
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            lines = [f"{stamp}  {who} moved {len(files)} raw file(s) to the Trash (kept {settings.keep_raw_months()} "
                     f"months; totals, history and certificates are kept):"] + [f"    {p.relative_to(f.path)}" for p in files]
            with open(f.work / "removed.txt", "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return self.state()

    def _trash(self, files: list[Path]) -> None:
        """Move to the Trash (recoverable), never delete outright."""
        if not files:
            return
        if sys.platform != "darwin":
            raise ck.CheckpointError("moving files to the Trash is only automatic on a Mac; remove them by hand")
        script = ('on run argv\n tell application "Finder"\n repeat with p in argv\n delete (POSIX file (p as text))\n'
                  ' end repeat\n end tell\nend run')
        done = subprocess.run(["osascript", "-e", script, *[str(p) for p in files]], capture_output=True, text=True)
        if done.returncode != 0:
            raise ck.CheckpointError(f"could not move the files to the Trash: {done.stderr.strip()}")

    def set_keep_raw(self, months: int) -> dict:
        months = int(months)
        if months < 1:
            raise ck.CheckpointError("keep at least 1 month")
        settings.save({"keep_raw_months": months})
        return self.state()

    def show_in_finder(self, month: str | None = None, what: str = "folder") -> None:
        if sys.platform != "darwin":
            raise ck.CheckpointError("only on a Mac")
        target = self.month(month).folder.scans if month and what == "scans" else \
            self.month(month).folder.path if month else self.ws.root
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(target)], check=True)


def _plain(e: Exception) -> str:
    """An error as a sentence a non-programmer can act on."""
    text = str(e)
    name = type(e).__name__
    if "authentication" in name.lower() or "401" in text:
        return "Claude did not accept the API key. Check it in Settings."
    if "permission" in name.lower() or "403" in text:
        return "The API key is not allowed to do this (check the key's limits in the Claude Console)."
    if "connection" in name.lower() or "timeout" in name.lower():
        return "Could not reach Claude (network). Check the internet connection and press Read again: " \
               "pages already read are kept."
    if "credit" in text.lower() or "billing" in text.lower():
        return "The Claude account is out of credit or over its spending limit."
    return text
