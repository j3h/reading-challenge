"""The review screen: a small local web page that shows each scanned sheet beside what was read from it,
and lets the operator settle every question (who is this, how many pages, which month, ...).

    python -m rc --data <folder> review --month "October 2026"

It listens on 127.0.0.1 only, on a random port, and every request needs a token that only the page it
served knows. Decisions are saved as they are made (work/decisions.json), so closing the browser or
stopping the program loses nothing. Text read from handwriting is only ever put on the page as text.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pymupdf

from rc import checkpoint as ck
from rc import matching as mt
from rc import review as rv
from rc import runner

PAGE = Path(__file__).with_name("review_table.html")    # the review screen: a table, one row per sheet
IMAGE_DPI = 110
STATUS_OPEN, STATUS_AUTO, STATUS_DECIDED, STATUS_SKIPPED, STATUS_HELD = "open", "auto", "decided", "skipped", "held"


class ReviewApp:
    """Everything the page needs, with no HTTP in it (so it can be tested directly)."""

    def __init__(self, month: runner.Month):
        self.month = month
        self._images: dict[str, bytes] = {}
        self._lock = threading.Lock()

    # ---- what the page shows
    def state(self) -> dict:
        m = self.month
        if m.folder.is_finished():
            return {"finished": True, "month": ck.month_label(m.folder.month),
                    "summary": m.folder.summary.read_text(encoding="utf-8") if m.folder.summary.exists() else "",
                    "files": {"certificates": str(m.folder.certificates), "summary": str(m.folder.summary),
                              "checkpoint": str(m.folder.checkpoint)}}
        cp, source = m.previous()
        plan = m.plan()
        decisions = m.decisions()
        grades = m.teacher_grades()
        recent: dict = {}                       # each student's last months, for the review row
        for h in cp.history:
            recent.setdefault(h.student_id, []).append([ck.month_label(h.month), h.pages])
        suggestions = rv.suggest(plan, cp, decisions, grades)
        teachers = sorted({s.teacher for s in cp.students.values() if s.teacher} | set(grades))
        open_keys = {i.key for i in plan.issues}
        sheets = []
        for item in plan.settled:
            r = item.record
            d = decisions.get(r.key, {})
            sug = suggestions.get(r.key)
            if item.skipped and not item.merged:
                status = STATUS_SKIPPED
            elif r.key in open_keys:
                status = STATUS_OPEN
            else:
                status = STATUS_DECIDED if d else STATUS_AUTO
            match = plan.matches.get(r.key)
            sheets.append({
                "key": r.key, "label": r.label, "file": r.file, "page": r.page,
                "read": {"name": r.name, "teacher": r.teacher, "month": r.month, "pages": r.pages,
                         "confidence": r.confidence, "pages_confidence": r.pages_confidence, "note": r.note,
                         "boxes": sorted(r.boxes), "form": r.is_form, "submitted": r.submitted, "grade": r.grade},
                "status": status, "decision": d, "outcome": self._outcome(item, cp, plan),
                "warnings": item.warnings,
                "reasons": sug.reasons if sug else [],
                "can_accept": bool(sug and sug.decision),
                "needs_choice": bool(sug and sug.needs_choice),
                "form": self._form(item, cp, d, sug, plan),
                "candidates": [self._cand(c, plan) for c in (match.candidates[:6] if match else [])],
                "duplicate": item.dup,
                "repeat": any(i.key == r.key and i.kind == "repeat" for i in plan.issues) or bool(d.get("repeat_ok")),
            })
        pool = isinstance(m, runner.Pool)
        if pool:                                  # held sheets: settled or not, kept out of rounds until brought back
            for r in m.held():
                d = decisions.get(r.key, {})
                item = rv.Settled(r, student_id=d.get("student") if d.get("student") in cp.students else None,
                                  pages=d.get("pages", r.pages))
                sheets.append({
                    "key": r.key, "label": r.label, "file": r.file, "page": r.page,
                    "read": {"name": r.name, "teacher": r.teacher, "month": r.month, "pages": r.pages,
                             "confidence": r.confidence, "pages_confidence": r.pages_confidence, "note": r.note,
                             "boxes": sorted(r.boxes), "form": r.is_form, "submitted": r.submitted, "grade": r.grade},
                    "status": STATUS_HELD, "decision": d, "outcome": "held for a later round",
                    "warnings": [], "reasons": [], "can_accept": False, "needs_choice": False,
                    "form": self._form(item, cp, d, None, plan), "candidates": [], "duplicate": None})
        counts = {k: sum(1 for s in sheets if s["status"] == k)
                  for k in (STATUS_OPEN, STATUS_AUTO, STATUS_DECIDED, STATUS_SKIPPED) + ((STATUS_HELD,) if pool else ())}
        extra = {}
        if pool:
            month = m.round_month
            extra = {"pool": True, "round_month": month, "round_label": ck.month_label(month),
                     "next_round": m.next_round_number(month), "round": m.counts()}
        return {
            **extra,
            "finished": False, "month": "Inbox" if pool else ck.month_label(m.folder.month), "source": source,
            "ready": (bool(extra["round"]["children"]) if pool else
                      plan.ready and any(s["status"] in (STATUS_AUTO, STATUS_DECIDED) for s in sheets)),
            "counts": counts, "acceptable": sum(1 for s in sheets if s["status"] == STATUS_OPEN and s["can_accept"]
                                                and not s["needs_choice"]),
            "sheets": sheets, "teachers": teachers, "teacher_grades": grades,
            "roster": [{"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade,
                        "nicknames": s.nicknames, "total": s.total, "new_from": self._new_from(s.id, plan),
                        "recent": recent.get(s.id, [])[-3:][::-1]}
                       for s in sorted(list(cp.students.values()) + list(plan.pending.values()),
                                       key=lambda s: s.name.lower())
                       if s.status == ck.ACTIVE],
        }

    @staticmethod
    def _new_from(sid, plan) -> str:
        """'p. 3' for a child being added as new from page 3 of this month's scans, else ''."""
        key = rv.new_from(sid)
        return next((f"p. {i.record.page}" for i in plan.settled if i.record.key == key), "") if key else ""

    @staticmethod
    def _form(item, cp, d, sug, plan) -> dict:
        """What the sheet's form shows: the person's decision, else what was settled, else the suggestion."""
        src = d if ("student" in d or "new_student" in d) else (sug.draft if sug else {})
        form = {"mode": "", "student": "", "new": {"name": item.record.name, "grade": ""}, "teacher": "",
                "pages": d.get("pages", item.pages if item.pages is not None else (sug.draft.get("pages") if sug else None)),
                "remember_name": bool(src.get("remember_name")), "duplicate": d.get("duplicate") or
                (sug.draft.get("duplicate") if sug else "") or ""}
        sid = src.get("student") or item.student_id
        if sid and sid in plan.pending:
            form.update(mode="student", student=sid, teacher=plan.pending[sid].teacher)
        elif sid and sid in cp.students:
            form.update(mode="student", student=sid, teacher=src.get("teacher") or cp.students[sid].teacher
                        or ((sug.draft.get("teacher") or "") if sug and sug.draft.get("student") == sid else ""))
        elif src.get("new_student") or item.new_student:
            n = src.get("new_student") or item.new_student
            form.update(mode="new", new={"name": n.get("name", ""), "grade": n.get("grade", "")},
                        teacher=n.get("teacher", ""))
        return form

    @classmethod
    def _cand(cls, c, plan) -> dict:
        s = c.student
        return {"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade, "score": c.score,
                "reason": c.reason, "total": s.total, "new_from": cls._new_from(s.id, plan)}

    @staticmethod
    def _outcome(item, cp, plan) -> str:
        if item.skipped:
            return item.skipped if item.merged else f"skipped: {item.skipped}"
        if item.student_id:
            s = plan.student(cp, item.student_id)
            new = " (new)" if item.student_id in plan.pending else ""
            return f"{s.name}{new} ({s.teacher or 'no teacher'})" + (f", pages {item.pages:,}" if item.pages is not None else "")
        if item.new_student:
            n = item.new_student
            return f"NEW: {n['name']} ({n.get('teacher') or 'no teacher yet'})"
        return ""

    # ---- what the operator can do
    def decide(self, key: str, decision: dict) -> dict:
        clean = {k: v for k, v in decision.items() if k in _DECISION_KEYS}
        with self._lock:
            self.month.set_decision(key, clean)
        return self.state()

    def accept_all(self) -> dict:
        """Accept every complete suggestion. Repeats while accepting reveals more (e.g. a duplicate)."""
        with self._lock:
            for _ in range(3):
                cp, _ = self.month.previous()
                decisions = self.month.decisions()
                sug = rv.suggest(self.month.plan(), cp, decisions, self.month.teacher_grades())
                new = {k: v.decision for k, v in sug.items()
                       if v.decision and not v.needs_choice and decisions.get(k) != v.decision}
                if not new:
                    break
                decisions.update(new)
                rv.save_decisions(decisions, self.month.decisions_path, "accept all")
        return self.state()

    def finish(self) -> dict:
        from rc import settings
        with self._lock:
            text = self.month.finish(who=settings.who())
        if isinstance(self.month, runner.Pool):
            return {**self.state(), "printed": text}
        return self.state()

    def _all_records(self):
        """Every sheet whose image may be asked for (in the inbox: printed and held ones too)."""
        m = self.month
        return m.all_records() if isinstance(m, runner.Pool) else m.records()

    # the month's finished files, by the name the page uses: (path, content type)
    def output(self, name: str) -> tuple[Path, str]:
        f = self.month.folder
        files = {"certificates.pdf": (f.certificates, "application/pdf"),
                 "summary.txt": (f.summary, "text/plain; charset=utf-8"),
                 "checkpoint.xlsx": (f.checkpoint,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        if name not in files or not f.is_finished():
            raise KeyError(name)
        return files[name]

    def open_locally(self, name: str) -> None:
        """Open a finished file in its usual app (Preview, Numbers/Excel), or the month's folder in Finder."""
        if sys.platform != "darwin":
            raise ck.CheckpointError("opening files in their app only works on a Mac; use the links instead")
        path = self.month.folder.path if name == "folder" else self.output(name)[0]
        subprocess.run(["open", str(path)], check=True)

    def scan_crop(self, key: str, field: str) -> bytes:
        """A snippet of the scan around where `field` was read, so it can sit next to the value on screen."""
        cache = f"{key}|{field}"
        with self._lock:
            if cache in self._images:
                return self._images[cache]
            rec = next((r for r in self._all_records() if r.key == key), None)
            if rec is None or field not in rec.boxes:
                raise KeyError(cache)
            x0, y0, x1, y1 = rec.boxes[field]
            padx, pady = max(25, (x1 - x0) * 0.3), max(15, (y1 - y0) * 0.6)   # the model's boxes are approximate
            page = pymupdf.open(self.month.folder.scans / rec.file)[rec.page - 1]   # PDFs and pictures alike
            w, h = page.rect.width, page.rect.height
            clip = pymupdf.Rect(max(0, x0 - padx) * w / 1000, max(0, y0 - pady) * h / 1000,
                                min(1000, x1 + padx) * w / 1000, min(1000, y1 + pady) * h / 1000)
            pix = page.get_pixmap(dpi=150, clip=clip, colorspace=pymupdf.csRGB)
            self._images[cache] = pix.tobytes("jpeg", jpg_quality=85)
            return self._images[cache]

    def scan_image(self, key: str) -> bytes:
        with self._lock:
            if key in self._images:
                return self._images[key]
            rec = next((r for r in self._all_records() if r.key == key), None)
            if rec is None or rec.is_form or not (self.month.folder.scans / rec.file).exists():
                raise KeyError(key)                   # unknown, typed in a form, or the scan was let go
            path = self.month.folder.scans / rec.file
            if path.suffix.lower() == ".pdf":
                pix = pymupdf.open(path)[rec.page - 1].get_pixmap(dpi=IMAGE_DPI, colorspace=pymupdf.csRGB)
            else:
                pix = pymupdf.Pixmap(str(path))
                if pix.colorspace is None or pix.n - pix.alpha != 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            self._images[key] = pix.tobytes("jpeg", jpg_quality=82)
            return self._images[key]


_DECISION_KEYS = {"student", "remember_name", "new_student", "skip", "pages", "pages_ok", "month_ok", "duplicate",
                  "teacher", "hold", "repeat_ok"}


# ---------------------------------------------------------------------------- HTTP
HOME_PAGE = Path(__file__).with_name("home.html")


def make_server(app: ReviewApp | None = None, port: int = 0, home=None) -> tuple[ThreadingHTTPServer, str]:
    """One month's review screen (`app`), or the whole app (`home`: rc.app.Home) with a home screen at /
    and each month's review at /review?m=2026-10."""
    token = secrets.token_urlsafe(16)

    def review_for(query) -> ReviewApp:
        if home is None:
            return app
        return home.review((query.get("m") or [""])[0])

    class Handler(BaseHTTPRequestHandler):
        server_version = "rc-review"

        def log_message(self, *args):       # quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body: bytes, ctype: str = "application/json", download: str | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", ("default-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                                                "font-src https://fonts.gstatic.com"))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def _page(self, path: Path):
            html = path.read_text(encoding="utf-8").replace("__TOKEN__", token)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _authorised(self, query) -> bool:
            sent = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return self._host_ok() and secrets.compare_digest(sent, token)

        # -- routes
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if url.path == "/ping":                    # the launcher checks whether the app is already running
                return self._send(200, b"rc-app", "text/plain")
            if url.path == "/":
                return self._page(HOME_PAGE if home is not None else PAGE)
            if url.path == "/review" and home is not None:
                return self._page(PAGE)
            if not self._authorised(query):
                return self._json(403, {"error": "bad token"})
            try:
                if url.path == "/api/home" and home is not None:
                    return self._json(200, home.state())
                if url.path == "/api/home/students" and home is not None:
                    return self._json(200, home.students())
                if url.path == "/api/home/teachers" and home is not None:
                    return self._json(200, home.teachers())
                if url.path == "/api/home/student" and home is not None:
                    return self._json(200, home.student(query["id"][0]))
                if url.path == "/api/state":
                    return self._json(200, review_for(query).state())
                if url.path == "/crop":
                    return self._send(200, review_for(query).scan_crop(query["key"][0], query["field"][0]), "image/jpeg")
                if url.path == "/scan":
                    return self._send(200, review_for(query).scan_image(query["key"][0]), "image/jpeg")
                if url.path == "/roundfile" and home is not None:
                    path = home.round_file(query["b"][0], query["name"][0])
                    ctype = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}.get(
                        path.suffix, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    return self._send(200, path.read_bytes(), ctype,
                                      download=None if ctype.startswith(("application/pdf", "text/")) else path.name)
                if url.path.startswith("/file/"):
                    path, ctype = review_for(query).output(url.path[len("/file/"):])
                    return self._send(200, path.read_bytes(), ctype,
                                      download=None if ctype.startswith(("application/pdf", "text/")) else path.name)
            except KeyError:
                return self._json(404, {"error": "no such sheet or file"})
            except ck.CheckpointError as e:
                return self._json(409, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorised(query):
                return self._json(403, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if url.path == "/api/home/upload" and home is not None:     # the body is the file itself
                    return self._json(200, home.add_scan(query["m"][0], query["name"][0], self.rfile, length))
                body = json.loads(self.rfile.read(length) or b"{}")
                if home is not None and url.path.startswith("/api/home/"):
                    return self._json(200, self._home_action(url.path[len("/api/home/"):], body))
                review = review_for(query)
                if url.path == "/api/decision":
                    return self._json(200, review.decide(body["key"], body.get("decision") or {}))
                if url.path == "/api/accept_all":
                    return self._json(200, review.accept_all())
                if url.path == "/api/finish":
                    return self._json(200, review.finish())
                if url.path == "/api/open":
                    review.open_locally(str(body.get("file", "")))
                    return self._json(200, {"ok": True})
            except (ck.CheckpointError, KeyError, ValueError, OSError, subprocess.CalledProcessError) as e:
                return self._json(409, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def _home_action(self, action: str, body: dict) -> dict:
            month = str(body.get("month", ""))
            if action == "folder":
                return home.choose_data_folder() if body.get("choose") else home.set_data_folder(str(body["path"]))
            if action == "key":
                from rc import settings
                settings.set_api_key(str(body.get("key", "")))
                return home.state()
            if action == "forget_key":
                from rc import settings
                settings.forget_api_key()
                return home.state()
            if action == "month":
                return home.create_month(month)
            if action == "read":
                return home.start_reading(month)
            if action == "reopen":
                return home.reopen(month, str(body.get("reason", "")))
            if action == "round_month":
                return home.set_round_month(month)
            if action == "round":
                return home.do_round()
            if action == "undo_round":
                return home.undo_round(str(body.get("batch", "")), str(body.get("reason", "")))
            if action == "to_inbox":
                return home.move_to_inbox()
            if action == "who":
                return home.set_who(str(body.get("name", "")))
            if action == "let_go":
                return home.let_go([str(x) for x in body.get("months", [])])
            if action == "keep_raw":
                return home.set_keep_raw(int(body.get("months", 0)))
            if action == "teacher_grade":
                return home.set_teacher_grade(str(body.get("teacher", "")), str(body.get("grade", "")),
                                              bool(body.get("align")), str(body.get("reason", "")))
            if action == "adjust":
                return home.adjust(str(body.get("student", "")), dict(body.get("change") or {}))
            if action == "finder":
                home.show_in_finder(month or None, str(body.get("what", "folder")))
                return home.state()
            if action == "quit":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return {"quit": True}
            raise KeyError(action)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server, token


def serve(month: runner.Month, open_browser: bool = True, port: int = 0) -> None:
    import webbrowser
    server, _ = make_server(ReviewApp(month), port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Review screen for {ck.month_label(month.folder.month)}: {url}\nPress Ctrl-C here to stop (nothing is lost).")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Decisions are saved in", month.decisions_path)
    finally:
        server.server_close()


def serve_app(home, open_browser: bool = True, port: int = 0) -> None:
    """The whole app: home screen, then each month's review."""
    import webbrowser
    server, _ = make_server(home=home, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Reading Challenge: {url}\nClose this window or press Ctrl-C to stop (nothing is lost).", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
