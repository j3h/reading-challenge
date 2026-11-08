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

PAGE = Path(__file__).with_name("review.html")
IMAGE_DPI = 110
STATUS_OPEN, STATUS_DONE, STATUS_SKIPPED = "open", "done", "skipped"


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
        by_key: dict[str, list] = {}
        for i in plan.issues:
            by_key.setdefault(i.key, []).append({"kind": i.kind, "message": i.message})
        teachers = sorted({s.teacher for s in cp.students.values() if s.teacher})
        sheets = []
        for item in plan.settled:
            r = item.record
            issues = by_key.get(r.key, [])
            status = STATUS_SKIPPED if item.skipped else STATUS_OPEN if issues else STATUS_DONE
            match = plan.matches.get(r.key)
            sheets.append({
                "key": r.key, "label": r.label, "file": r.file, "page": r.page,
                "read": {"name": r.name, "teacher": r.teacher, "month": r.month, "pages": r.pages,
                         "confidence": r.confidence, "pages_confidence": r.pages_confidence, "note": r.note},
                "status": status, "issues": issues, "decision": decisions.get(r.key, {}),
                "outcome": self._outcome(item, cp),
                "candidates": [self._cand(c) for c in (match.candidates[:6] if match else [])],
                "match_status": match.status if match else mt.NONE,
                # the roster teacher the sheet's teacher most likely means ("Ms. Tracy" -> "Tracy")
                "teacher_guess": next((t for t in teachers if mt.teacher_agreement(r.teacher, t) == "agree"), ""),
            })
        order = {STATUS_OPEN: 0, STATUS_DONE: 1, STATUS_SKIPPED: 2}
        return {
            "finished": False, "month": ck.month_label(m.folder.month), "source": source,
            "ready": plan.ready and any(s["status"] == STATUS_DONE for s in sheets),
            "counts": {k: sum(1 for s in sheets if s["status"] == k) for k in order},
            "sheets": sheets,
            "teachers": teachers,
            "roster": [{"id": s.id, "name": s.name, "teacher": s.teacher, "grade": s.grade,
                        "nicknames": s.nicknames, "total": s.total}
                       for s in sorted(cp.students.values(), key=lambda s: s.name.lower())
                       if s.status == ck.ACTIVE],
        }

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

    def finish(self) -> dict:
        with self._lock:
            self.month.finish()
        return self.state()

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
