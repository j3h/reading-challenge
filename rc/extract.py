"""Read the scanned reading sheets with Claude (vision) and turn each into a review.SheetRecord.

    op run --env-file=.env -- python -m rc --data <folder> extract --month "October 2026"

The API key never touches the code or the repo: `.env` holds only a 1Password reference and `op run`
puts the key in the environment for this one command.

Every page's raw answer is saved in the month's work/raw/ folder, so re-running (or changing how answers
are turned into records) costs nothing; only pages without a saved answer are sent. If any page fails, no
records.json is written and the failures are listed: fix the cause and run it again.

What the model is asked for follows the rules settled earlier: use the child's own written total for the
month (inside the "total pages" box, else the nearest number to it, ignoring crossed-out ones), report
conflicting totals instead of choosing silently, and add up the book rows only when no total was written.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

from rc import checkpoint as ck
from rc import review as rv
from rc import workspace as wsp

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_DPI = 150
PRICES = {  # $ per million tokens (input, output) for the cost estimate
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

CONFIDENCE = ["high", "low", "unreadable"]
SCHEMA = {
    "type": "object",
    "properties": {
        "is_reading_sheet": {"type": "boolean"},
        "name": {"type": "string"},
        "name_confidence": {"type": "string", "enum": CONFIDENCE},
        "teacher": {"type": "string"},
        "teacher_confidence": {"type": "string", "enum": CONFIDENCE},
        "month": {"type": "string"},
        "has_total": {"type": "boolean"},
        "total_pages": {"type": "integer"},
        "total_confidence": {"type": "string", "enum": CONFIDENCE},
        "total_where": {"type": "string"},
        "other_total_candidates": {"type": "array", "items": {"type": "integer"}},
        "rows": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "pages": {"type": "integer"}},
            "required": ["title", "pages"], "additionalProperties": False}},
        "note": {"type": "string"},
    },
    "required": ["is_reading_sheet", "name", "name_confidence", "teacher", "teacher_confidence", "month",
                 "has_total", "total_pages", "total_confidence", "total_where", "other_total_candidates",
                 "rows", "note"],
    "additionalProperties": False,
}

PROMPT = """This is one scanned page from a school reading challenge. A child fills in a sheet each month: their
name, their teacher, the month, the books they read with page counts, and a total number of pages read that
month. Read the page and report what is written.

- is_reading_sheet: false if the page is blank, a cover page, or not one of these sheets (then leave the other
  fields empty or 0).
- name and teacher: exactly as written (first name only is common; do not guess a last name). Empty string if
  not written. Give a confidence for each: "high" if clear, "low" if you had to guess any letter, "unreadable"
  if you could not read it.
- month: as written on the sheet (e.g. "October", "9/19", "September 2026"); empty string if none.
- total_pages: the child's own written total of pages read THIS MONTH. Prefer the number inside the box
  labelled for the total (e.g. "TOTAL PAGES READ THIS MONTH" or "PAGES READ"); if there is no box, the number
  written nearest that label or near the signature line. Ignore a crossed-out number and use the corrected one.
  Never add up the book rows yourself to make a total, and do not "fix" a total that disagrees with the rows:
  report what the child wrote. If no total is written at all, set has_total to false and total_pages to 0.
- total_confidence: "low" if any digit is uncertain or if you found two different candidate totals; list the
  other candidate number(s) in other_total_candidates. total_where: a few words saying where you found it.
- rows: each book row with its title and the pages written for it (0 if the pages are unreadable).
- note: only something that makes a name, teacher or total hard to trust, in one short sentence (a doubtful
  digit, a crossed-out total you replaced, a total that clearly belongs to another month). Do not comment on
  the date or month, on rows that do not add up to the total, or on how tidy the sheet is. Empty string if
  nothing needs checking.
"""


# ------------------------------------------------------------------ rendering
def parse_page_range(text: str | None, count: int) -> list[int]:
    """'1-10', '3', '2,5-7' -> sorted 1-based page numbers within `count`. None means all."""
    if not text:
        return list(range(1, count + 1))
    pages: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", part)
        if not m:
            raise ck.CheckpointError(f"cannot read page range {text!r}; use e.g. 1-10 or 3,5-7")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        pages.update(range(a, b + 1))
    bad = [p for p in pages if not 1 <= p <= count]
    if bad:
        raise ck.CheckpointError(f"page(s) {sorted(bad)} are outside this scan, which has {count} page(s)")
    return sorted(pages)


def render_pages(path: Path, pages: str | None, dpi: int):
    """Yield (page number, jpeg bytes) for a PDF, or the single image for a picture file."""
    if path.suffix.lower() == ".pdf":
        doc = pymupdf.open(path)
        for n in parse_page_range(pages, doc.page_count):
            pix = doc[n - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB)
            yield n, pix.tobytes("jpeg", jpg_quality=85)
    else:
        pix = pymupdf.Pixmap(str(path))
        if pix.colorspace is None or pix.n - pix.alpha != 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        yield 1, pix.tobytes("jpeg", jpg_quality=85)


# ------------------------------------------------------------------ calling the model
class ExtractionError(ck.CheckpointError):
    pass


def ask(client, jpeg: bytes, model: str):
    """One page -> (parsed answer dict, usage dict). Raises ExtractionError for anything but a clean answer."""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(jpeg).decode("ascii")}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("the model declined to read this page")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("the answer was cut off (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ExtractionError(f"the answer was not valid JSON: {text[:80]!r}") from None
    usage = {"input_tokens": getattr(response.usage, "input_tokens", 0),
             "output_tokens": getattr(response.usage, "output_tokens", 0)}
    return data, usage


# ------------------------------------------------------------------ raw answers -> records
def _worst(*levels: str) -> str:
    order = {rv.HIGH: 0, rv.LOW: 1, rv.UNREADABLE: 2}
    return max(levels, key=lambda lv: order.get(lv, 2))


def to_record(file: str, page: int, a: dict) -> rv.SheetRecord | None:
    """None for a page that is not a reading sheet."""
    if not a.get("is_reading_sheet", True):
        return None
    notes = [a["note"].strip()] if a.get("note", "").strip() else []
    row_total = sum(max(r["pages"], 0) for r in a.get("rows", []))
    others = sorted({n for n in a.get("other_total_candidates", []) if n != a.get("total_pages")})
    if a.get("has_total"):
        pages, conf = int(a["total_pages"]), a["total_confidence"]
        if others:
            conf = _worst(conf, rv.LOW)
            notes.insert(0, f"two totals: {pages:,} vs {', '.join(f'{n:,}' for n in others)}")
    elif row_total > 0:     # no written total: fall back to the rows, but a person confirms it
        pages, conf = row_total, rv.LOW
        notes.insert(0, f"no total written; added up the rows ({row_total:,})")
    else:
        pages, conf = None, rv.UNREADABLE
        notes.insert(0, "no total and no readable rows")
    who = _worst(a["name_confidence"], a["teacher_confidence"] if a["teacher"].strip() else rv.HIGH)
    if not a["name"].strip():
        who = rv.UNREADABLE
    return rv.SheetRecord(file, page, a["name"].strip(), a["teacher"].strip(), a["month"].strip(), pages,
                          confidence=who, pages_confidence=conf, note="; ".join(notes))


# ------------------------------------------------------------------ a whole month
def raw_path(raw_dir: Path, file: str, page: int) -> Path:
    return raw_dir / f"{Path(file).stem}.p{page:04d}.json"


def extract_month(client, month_folder: wsp.MonthFolder, model: str = DEFAULT_MODEL, dpi: int = DEFAULT_DPI,
                  pages: str | None = None, force: bool = False, workers: int = 4, log=print):
    """Read every scan in the month folder. Returns (records, dropped, usage); writes work/records.json."""
    files = month_folder.scan_files()
    if not files:
        raise ck.CheckpointError(f"no scans in {month_folder.scans}")
    stray = wsp.cloud_placeholders(month_folder.scans)
    if stray:
        raise ck.CheckpointError(f"{len(stray)} scan(s) are still in iCloud and not downloaded yet; "
                                 f"open the folder in Finder and wait for them")
    raw_dir = month_folder.work / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(f.name, n, jpeg) for f in files for n, jpeg in render_pages(f, pages, dpi)]

    def run(job):
        file, n, jpeg = job
        cached = raw_path(raw_dir, file, n)
        if cached.exists() and not force:
            return job[:2], json.loads(cached.read_text(encoding="utf-8")), None, False
        try:
            data, usage = ask(client, jpeg, model)
        except ExtractionError as e:
            return job[:2], None, str(e), False
        except Exception as e:  # network, rate limit after the SDK's own retries, bad key, ...
            return job[:2], None, f"{type(e).__name__}: {e}", False
        saved = {"model": model, "dpi": dpi, "usage": usage, "answer": data}
        wsp.write_atomic(cached, json.dumps(saved, indent=2, ensure_ascii=False))
        log(f"  read {file} p. {n}")
        return job[:2], saved, None, True

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(run, jobs))

    failures = [(key, err) for key, saved, err, _ in results if err]
    if failures:
        lines = [f"  {f} p. {n}: {err}" for (f, n), err in failures]
        raise ExtractionError(f"{len(failures)} page(s) could not be read; nothing was saved as records. "
                              f"Fix the cause and run again (pages already read are kept):\n" + "\n".join(lines))
    records, dropped = [], []
    usage = {"input_tokens": 0, "output_tokens": 0, "pages_sent": 0}
    for (file, n), saved, _, fresh in results:
        if fresh:      # cost covers only pages sent this run; answers kept from earlier runs cost nothing now
            usage["input_tokens"] += saved["usage"]["input_tokens"]
            usage["output_tokens"] += saved["usage"]["output_tokens"]
            usage["pages_sent"] += 1
        rec = to_record(file, n, saved["answer"])
        (records.append(rec) if rec else dropped.append(f"{file} p. {n}"))
    rv.save_records(records, month_folder.work / "records.json")
    return records, dropped, usage


def dry_run(month_folder: wsp.MonthFolder, pages: str | None = None, dpi: int = DEFAULT_DPI):
    """Render without calling the API. Returns (page count, total bytes, largest image bytes, already read)."""
    raw_dir = month_folder.work / "raw"
    count = total = largest = done = 0
    for f in month_folder.scan_files():
        for n, jpeg in render_pages(f, pages, dpi):
            count, total, largest = count + 1, total + len(jpeg), max(largest, len(jpeg))
            done += raw_path(raw_dir, f.name, n).exists()
    return count, total, largest, done


def cost_estimate(usage: dict, model: str) -> str:
    if model not in PRICES:
        return f"{usage['input_tokens']:,} input + {usage['output_tokens']:,} output tokens"
    pin, pout = PRICES[model]
    dollars = usage["input_tokens"] * pin / 1e6 + usage["output_tokens"] * pout / 1e6
    return (f"{usage['input_tokens']:,} input + {usage['output_tokens']:,} output tokens "
            f"= about ${dollars:.2f} for {usage['pages_sent']} page(s) sent")


def make_client():
    import anthropic
    return anthropic.Anthropic(max_retries=4)
