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
STATUS_OPEN, STATUS_AUTO, STATUS_DECIDED, STATUS_SKIPPED = "open", "auto", "decided", "skipped"


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
        suggestions = rv.suggest(plan, cp, decisions)
        teachers = sorted({s.teacher for s in cp.students.values() if s.teacher})
        open_keys = {i.key for i in plan.issues}
        sheets = []
        for item in plan.settled:
            r = item.record
            d = decisions.get(r.key, {})
            sug = suggestions.get(r.key)
            if item.skipped:
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
                         "boxes": sorted(r.boxes)},
                "status": status, "decision": d, "outcome": self._outcome(item, cp),
                "warnings": item.warnings,
                "reasons": sug.reasons if sug else [],
                "can_accept": bool(sug and sug.decision),
                "form": self._form(item, cp, d, sug),
                "candidates": [self._cand(c) for c in (match.candidates[:6] if match else [])],
                "duplicate": any(i.kind == "duplicate" and i.key == r.key for i in plan.issues) or "duplicate" in d,
            })
        counts = {k: sum(1 for s in sheets if s["status"] == k)
                  for k in (STATUS_OPEN, STATUS_AUTO, STATUS_DECIDED, STATUS_SKIPPED)}
        return {
            "finished": False, "month": ck.month_label(m.folder.month), "source": source,
            "ready": plan.ready and any(s["status"] in (STATUS_AUTO, STATUS_DECIDED) for s in sheets),
            "counts": counts, "acceptable": sum(1 for s in sheets if s["status"] == STATUS_OPEN and s["can_accept"]),
            "sheets": sheets, "teachers": teachers,
            "roster": [{"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade,
                        "nicknames": s.nicknames, "total": s.total}
                       for s in sorted(cp.students.values(), key=lambda s: s.name.lower())
                       if s.status == ck.ACTIVE],
        }

    @staticmethod
    def _form(item, cp, d, sug) -> dict:
        """What the sheet's form shows: the person's decision, else what was settled, else the suggestion."""
        src = d if ("student" in d or "new_student" in d) else (sug.draft if sug else {})
        form = {"mode": "", "student": "", "new": {"name": item.record.name, "grade": ""}, "teacher": "",
                "pages": d.get("pages", item.pages if item.pages is not None else (sug.draft.get("pages") if sug else None)),
                "remember_name": bool(src.get("remember_name")), "duplicate": d.get("duplicate") or
                (sug.draft.get("duplicate") if sug else "") or ""}
        sid = src.get("student") or item.student_id
        if sid and sid in cp.students:
            form.update(mode="student", student=sid, teacher=src.get("teacher") or cp.students[sid].teacher
                        or ((sug.draft.get("teacher") or "") if sug and sug.draft.get("student") == sid else ""))
        elif src.get("new_student") or item.new_student:
            n = src.get("new_student") or item.new_student
            form.update(mode="new", new={"name": n.get("name", ""), "grade": n.get("grade", "")},
                        teacher=n.get("teacher", ""))
        return form

    @staticmethod
    def _cand(c) -> dict:
        s = c.student
        return {"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade, "score": c.score,
                "reason": c.reason, "from_hints": c.from_hints, "total": s.total}

    @staticmethod
    def _outcome(item, cp) -> str:
        if item.skipped:
            return f"skipped: {item.skipped}"
        if item.student_id:
            s = cp.students[item.student_id]
            return f"{s.name} ({s.teacher or 'no teacher'})" + (f", pages {item.pages:,}" if item.pages is not None else "")
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
                sug = rv.suggest(self.month.plan(), cp, decisions)
                new = {k: v.decision for k, v in sug.items() if v.decision and decisions.get(k) != v.decision}
                if not new:
                    break
                decisions.update(new)
                rv.save_decisions(decisions, self.month.decisions_path)
        return self.state()

    def finish(self) -> dict:
        with self._lock:
            self.month.finish()
        return self.state()

    def scan_crop(self, key: str, field: str) -> bytes:
        """A snippet of the scan around where `field` was read, so it can sit next to the value on screen."""
        cache = f"{key}|{field}"
        with self._lock:
            if cache in self._images:
                return self._images[cache]
            rec = next((r for r in self.month.records() if r.key == key), None)
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
            rec = next((r for r in self.month.records() if r.key == key), None)
            if rec is None:
                raise KeyError(key)
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
                  "teacher"}


# ---------------------------------------------------------------------------- HTTP
def make_server(app: ReviewApp, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        server_version = "rc-review"

        def log_message(self, *args):       # quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def _authorised(self, query) -> bool:
            sent = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return self._host_ok() and secrets.compare_digest(sent, token)

        # -- routes
        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if url.path == "/":
                html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if not self._authorised(query):
                return self._json(403, {"error": "bad token"})
            try:
                if url.path == "/api/state":
                    return self._json(200, app.state())
                if url.path == "/crop":
                    return self._send(200, app.scan_crop(query["key"][0], query["field"][0]), "image/jpeg")
                if url.path == "/scan":
                    return self._send(200, app.scan_image(query["key"][0]), "image/jpeg")
            except KeyError:
                return self._json(404, {"error": "no such sheet"})
            except ck.CheckpointError as e:
                return self._json(409, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authorised({}):
                return self._json(403, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if url.path == "/api/decision":
                    return self._json(200, app.decide(body["key"], body.get("decision") or {}))
                if url.path == "/api/accept_all":
                    return self._json(200, app.accept_all())
                if url.path == "/api/finish":
                    return self._json(200, app.finish())
            except (ck.CheckpointError, KeyError, ValueError) as e:
                return self._json(409, {"error": str(e)})
            self._json(404, {"error": "not found"})

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
