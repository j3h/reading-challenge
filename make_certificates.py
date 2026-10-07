#!/usr/bin/env python3
"""Generate reading-challenge certificates from a CSV.

    python make_certificates.py kids.csv --month "October 2026"

CSV columns (header row required):
    name          Child's name
    pages         Pages read this month            (3194 or 3,194)
    total_pages   Grand total pages read so far (including this month)
    prize_levels  Optional. Normally leave out: the levels are worked out from
                  the page counts. If filled in, names separated by ';' replace
                  the computed ones for that row.
    month         Optional. Overrides --month for that row (e.g. "October 2026")

Prize levels come from levels.csv (threshold, label, prize). A child reaches a
level in the month her running total crosses it, i.e.
    (total_pages - pages) < threshold <= total_pages

Output (in --out, default ./out):
    <Name>.pdf            one certificate per child
    all_certificates.pdf  everything in one file, for printing
    prizes.csv            what each child has earned this month (for handing out)

How alignment works
-------------------
Every line of text is centered on CENTER_X and sits on its own baseline Y
(PDF points, measured from the TOP of the page, like Canva/PyMuPDF report).
Before drawing, each line is measured with the real font, and:
  * single lines (name, pages)  shrink until they fit MAX_WIDTH
  * the prize sentence          wraps to several lines, and if the block
                                would run past BOTTOM_LIMIT the font and
                                line spacing shrink together until it fits
Nothing is positioned by hand per child, so any text stays centered.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Layout, taken from the original Canva export (letter landscape, 792 x 612)
# --------------------------------------------------------------------------
BACKGROUND = HERE / "background_blank.pdf"
FONT_BODY = HERE / "fonts" / "LexendDeca-Regular.ttf"
FONT_NAME = HERE / "fonts" / "LeckerliOne-Regular.ttf"

CENTER_X = 396.0      # horizontal center of the page / white panel
MAX_WIDTH = 600.0     # widest any line may be (white panel is ~660 wide)
BOTTOM_LIMIT = 515.0  # lowest allowed baseline (white panel ends ~540)

BLACK = (0, 0, 0)
NAME_COLOR = (0x18 / 255, 0x00 / 255, 0xAD / 255)

TITLE = "Beverly Cleary Reading Challenge"
INTRO = "has been awarded this certificate for reading"

# y = baseline measured from top of page; size = starting font size in pt
TITLE_Y, TITLE_SIZE = 167.9, 38.0
NAME_Y, NAME_SIZE, NAME_MIN = 273.1, 60.0, 30.0
# Two-line fallback for very long names (baseline of the LAST line, sizes, leading ratio)
NAME2_BOTTOM_Y, NAME2_SIZE, NAME2_MIN, NAME2_LEADING = 276.0, 40.0, 26.0, 1.12
INTRO_Y, INTRO_SIZE = 340.1, 21.0
PAGES_Y, PAGES_SIZE, PAGES_MIN = 390.3, 41.0, 24.0
PRIZE_Y, PRIZE_SIZE, PRIZE_MIN, PRIZE_LEADING = 443.3, 21.6, 14.0, 30.0
# The grand-total line always follows the prize block at the same leading.


LEVELS_FILE = HERE / "levels.csv"


def to_int(text):
    return int(re.sub(r"[,\s]", "", text))


def load_levels(path=LEVELS_FILE):
    """[(threshold, label, prize), ...] sorted by threshold."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        table = [(to_int(r["threshold"]), r["label"].strip(), r["prize"].strip())
                 for r in csv.DictReader(fh)]
    return sorted(table)


def resolve_levels(row, table):
    """Return (labels, prizes) for one CSV row.
    Computed from the page counts unless the row has its own prize_levels."""
    pages, total = to_int(row["pages"]), to_int(row["total_pages"])
    manual = [s.strip() for s in (row.get("prize_levels") or "").split(";") if s.strip()]
    if manual:
        return manual, []
    previous = total - pages
    if previous < 0:
        raise ValueError(f"total_pages ({total}) is smaller than pages this month ({pages})")
    crossed = [(label, prize) for threshold, label, prize in table if previous < threshold <= total]
    return [c[0] for c in crossed], [c[1] for c in crossed]


def prize_tokens(levels):
    """The prize sentence as a list of units that must not be split across lines.
    One level:    You've reached the A prize level!
    Two levels:   You've reached the A and B prize levels!
    Three+:       You've reached the A, B, and C prize levels!
    Each level name stays whole, and 'the' / 'and' stay glued to the name after them."""
    words = ["You\u2019ve", "reached"]
    if len(levels) == 1:
        units = [f"the {levels[0]}", "prize level!"]
    elif len(levels) == 2:
        units = [f"the {levels[0]}", f"and {levels[1]}", "prize levels!"]
    else:
        units = [f"the {levels[0]},"] + [f"{lv}," for lv in levels[1:-1]] + [f"and {levels[-1]}", "prize levels!"]
    return words + units


# --------------------------------------------------------------------------
class Typesetter:
    def __init__(self, page, doc):
        self.page = page
        self.fonts = {}
        for key, path in (("body", FONT_BODY), ("name", FONT_NAME)):
            fname = f"F_{key}"
            page.insert_font(fontname=fname, fontfile=str(path))
            self.fonts[key] = (fname, pymupdf.Font(fontfile=str(path)))

    def width(self, key, text, size):
        return self.fonts[key][1].text_length(text, fontsize=size)

    def fit_size(self, key, text, size, min_size):
        """Largest size <= `size` for which text fits MAX_WIDTH."""
        w = self.width(key, text, size)
        if w <= MAX_WIDTH:
            return size, False
        shrunk = max(min_size, size * MAX_WIDTH / w)
        return shrunk, self.width(key, text, shrunk) > MAX_WIDTH + 0.01  # still too wide?

    def centered(self, key, text, y, size, color=BLACK):
        x = CENTER_X - self.width(key, text, size) / 2
        self.page.insert_text((x, y), text, fontname=self.fonts[key][0],
                              fontsize=size, color=color)

    def _greedy(self, key, tokens, size, limit):
        lines, cur = [], ""
        for word in tokens:
            trial = f"{cur} {word}".strip()
            if cur and self.width(key, trial, size) > limit:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def wrap(self, key, tokens, size):
        """Wrap a list of unbreakable tokens to MAX_WIDTH, then narrow the limit as
        far as possible without adding a line, so lines are balanced (no orphans)."""
        lines = self._greedy(key, tokens, size, MAX_WIDTH)
        n = len(lines)
        if n == 1:
            return lines
        lo, hi = MAX_WIDTH / n, MAX_WIDTH
        for _ in range(20):
            mid = (lo + hi) / 2
            if len(self._greedy(key, tokens, size, mid)) <= n:
                hi = mid
            else:
                lo = mid
        return self._greedy(key, tokens, size, hi)


def draw_certificate(doc, row, levels, warn):
    page = doc.new_page(width=792, height=612)
    page.show_pdf_page(page.rect, pymupdf.open(BACKGROUND), 0)
    t = Typesetter(page, doc)

    name = row["name"].strip()
    pages = to_int(row["pages"])
    total = to_int(row["total_pages"])
    month = (row.get("month") or "").strip() or row["_default_month"]

    t.centered("body", TITLE, TITLE_Y, TITLE_SIZE)

    size, overflow = t.fit_size("name", name, NAME_SIZE, NAME_MIN)
    if not overflow:
        if size < NAME_SIZE:
            warn(f"{name!r}: name shrunk to {size:.0f}pt")
        t.centered("name", name, NAME_Y, size, NAME_COLOR)
    else:
        # Too long for one line even at NAME_MIN: use two lines instead.
        # The second line sits where the single line would, so it stays clear
        # of the decorative underline; the first line goes above it.
        size = NAME2_SIZE
        while True:
            lines = t.wrap("name", name.split(), size)
            if (len(lines) <= 2 and all(t.width("name", ln, size) <= MAX_WIDTH for ln in lines)) \
                    or size <= NAME2_MIN:
                break
            size -= 0.5
        if len(lines) > 2 or any(t.width("name", ln, size) > MAX_WIDTH for ln in lines):
            warn(f"{name!r}: name does not fit on two lines even at {NAME2_MIN}pt")
        warn(f"{name!r}: long name wrapped onto {len(lines)} lines at {size:.0f}pt")
        leading = NAME2_LEADING * size
        for i, ln in enumerate(lines):
            y = NAME2_BOTTOM_Y - leading * (len(lines) - 1 - i)
            t.centered("name", ln, y, size, NAME_COLOR)

    t.centered("body", INTRO, INTRO_Y, INTRO_SIZE)

    pages_text = f"{pages:,} {'Page' if pages == 1 else 'Pages'}"
    size, _ = t.fit_size("body", pages_text, PAGES_SIZE, PAGES_MIN)
    t.centered("body", pages_text, PAGES_Y, size)

    total_text = f"Grand Total: {total:,} {'page' if total == 1 else 'pages'} read as of {month}"
    tokens = prize_tokens(levels) if levels else []

    # Find the largest font size where prize lines + total line fit above BOTTOM_LIMIT.
    size = PRIZE_SIZE
    while True:
        scale = size / PRIZE_SIZE
        leading = PRIZE_LEADING * scale
        lines = t.wrap("body", tokens, size) if tokens else []
        lines.append(total_text)
        too_wide = any(t.width("body", ln, size) > MAX_WIDTH for ln in lines)
        last_baseline = PRIZE_Y + leading * (len(lines) - 1)
        if (last_baseline <= BOTTOM_LIMIT and not too_wide) or size <= PRIZE_MIN:
            break
        size -= 0.25
    if size < PRIZE_SIZE:
        warn(f"{name!r}: prize block shrunk to {size:.1f}pt ({len(lines)} lines)")
    if last_baseline > BOTTOM_LIMIT or too_wide:
        warn(f"{name!r}: prize block does not fit even at {PRIZE_MIN}pt - shorten the level names")
    for i, ln in enumerate(lines):
        t.centered("body", ln, PRIZE_Y + leading * i, size)
    return page


def safe_filename(name, used):
    base = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_") or "certificate"
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f"{base}_{n}", n + 1
    used.add(candidate)
    return candidate + ".pdf"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_file")
    ap.add_argument("--month", default="", help='e.g. "October 2026" (used when a row has no month column)')
    ap.add_argument("--out", default="out", help="output folder (default: ./out)")
    args = ap.parse_args()

    with open(args.csv_file, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    required = {"name", "pages", "total_pages"}
    missing = required - set(rows[0].keys() if rows else required)
    if missing:
        sys.exit(f"CSV is missing column(s): {', '.join(sorted(missing))}")
    if not args.month and not all((r.get("month") or "").strip() for r in rows):
        sys.exit('Provide --month "October 2026" or a month column on every row')

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warnings = []
    warn = warnings.append
    used, combined = set(), pymupdf.open()
    table = load_levels()
    report = []

    for i, row in enumerate(rows, start=2):  # line numbers as in the CSV file
        row["_default_month"] = args.month
        try:
            levels, prizes = resolve_levels(row, table)
            single = pymupdf.open()
            draw_certificate(single, row, levels, warn)
            draw_certificate(combined, row, levels, lambda m: None)  # warnings already recorded
        except (ValueError, KeyError) as e:
            sys.exit(f"CSV line {i}: could not read row ({e!r}): {row}")
        if not levels:
            warn(f"{row['name'].strip()!r}: no new prize level this month (certificate has no prize line)")
        single.subset_fonts()
        single.save(out / safe_filename(row["name"], used), garbage=4, deflate=True)
        single.close()
        report.append({"name": row["name"].strip(), "pages_this_month": to_int(row["pages"]),
                       "total_pages": to_int(row["total_pages"]),
                       "levels": "; ".join(levels), "prizes": "; ".join(prizes)})

    combined.subset_fonts()
    combined.save(out / "all_certificates.pdf", garbage=4, deflate=True)
    with open(out / "prizes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(report[0].keys()))
        w.writeheader()
        w.writerows(report)
    print(f"Wrote {len(rows)} certificates, all_certificates.pdf and prizes.csv to {out}/")
    for w in warnings:
        print("  note:", w)


if __name__ == "__main__":
    main()
