# Reading Challenge Certificates

Generates the monthly Beverly Cleary Reading Challenge certificates (letter landscape PDF)
from a CSV of children's page counts. Replaces a Canva "bulk create" workflow; works offline.

## Commands
- Setup: `pip install -r requirements.txt`
- Generate: `python make_certificates.py kids.csv --month "October 2026"` -> `out/` (one PDF per
  child, `all_certificates.pdf` for printing, `prizes.csv` listing prizes owed)
- From the tracker workbook: export the Roster, Entries and Prizes tabs as CSV, then
  `python make_certificates.py --roster roster.csv --entries entries.csv --prizes prizes.csv --month "October 2026"`
  (certificate lists EVERY level owed; `out/unresolved.csv` lists entries not yet matched to a child)
- Try it: `python make_certificates.py sample.csv --month "October 2026"`
- Test: `python -m pytest -q`
- New Canva design: `python tools/make_blank_background.py export.pdf` (rewrites
  `background_blank.pdf` and prints text positions/sizes/colours to copy into the constants)

## Layout
- `make_certificates.py`  everything: CSV parsing, level logic, typesetting, output
- `tracker.py`            tracker CSV exports -> certificate rows (void, latest-row-wins, owed levels)
- `levels.csv`            prize chart: threshold, label, prize (edit this, not the code)
- `background_blank.pdf`  Canva export with all text removed (stripes, books, borders only)
- `fonts/`                Lexend Deca Regular (static instance) and Leckerli One, both OFL
- `tracker_template.xlsx`  blank data-entry workbook (upload to Google Sheets); rebuilt by
                          `python tools/make_tracker_template.py`. Entries/Prizes/Roster/Find/Levels tabs;
                          unresolved forms stay in Entries with a blank Child until Jamie identifies them
- `tests/`                level logic + alignment checks; `sample.csv` demo input

## How it works (read before changing layout code)
- A child reaches a level in the month her running total crosses it:
  `(total_pages - pages) < threshold <= total_pages`. The CSV's `total_pages` includes this month.
  An optional `prize_levels` column (`;`-separated) overrides this for that row.
- Alignment: every line is centred on `CENTER_X` (396pt) at its own baseline. Y values are in
  points from the TOP of the page and were measured from the original Canva export.
  Per-child text is measured with the real font: name and page count shrink to `MAX_WIDTH`;
  a name that still doesn't fit wraps to two lines; the prize sentence wraps on whole level names
  (never splits "2,500 Pages") with balanced lines, shrinking font and leading together if the
  block would pass `BOTTOM_LIMIT`.
- The title is fixed text and is deliberately wider than `MAX_WIDTH` (matches the Canva design).
- Text is drawn with PyMuPDF; fonts are embedded and subset, text stays selectable.

## Tracker mode (read before changing tracker.py or the workbook)
- `tracker.py` mirrors the formulas in `tracker_template.xlsx`; change both together.
- Owed levels = every level with `last_prize_given < threshold <= total_as_of_month`. This
  replaces the single-CSV rule ("crossed this month") so a skipped month never loses a prize.
- Entries without a Child ID on the roster are never counted; they are printed and written to
  `out/unresolved.csv`. Sheet exports contain formula-only rows, so blank rows are detected from
  the typed columns only.
- Children owed prizes but with no entry this month get a note, not a certificate.

## Conventions and gotchas
- Verify layout changes visually, not only with tests: render a page
  (`page.get_pixmap(dpi=100).save(...)`) and look at it. Test with a very long name, one
  prize level, six levels, and no levels.
- Lexend Deca ships as a variable font; `fonts/LexendDeca-Regular.ttf` is the pinned wght=400
  instance (made with fontTools). Don't swap the variable file in: ReportLab/PyMuPDF would use
  the Thin default.
- No kerning is applied; widths still matched the Canva export within 0.5pt.
- Row errors (bad numbers, total < pages) stop the run with the CSV line number. Warnings
  (shrunk/wrapped text, no new level) print at the end of a run: read them.
- **Privacy:** the real CSVs contain children's names. `.gitignore` excludes every `*.csv`
  except `sample.csv` and `levels.csv`, plus `out/` and generated PDFs. Never commit real
  names or output; keep `sample.csv` to made-up or already-public data.

## New pipeline (`rc/`, in progress): scans -> checkpoint + classroom print PDF
Replaces the CSV/tracker flow. Built backwards from the printout; all pieces below exist and are tested
except extraction (OCR), the review UI and the one-command runner.
- `rc/checkpoint.py`  students and running totals; `apply_month()`; xlsx save/load. **Prizes are the levels a
  month's pages cross (previous < threshold <= new total), exactly what the certificate shows**; nothing is
  carried over, so a reported mistake is fixed by hand (edit the total, hand over the prize). A month is applied once and only after the previous one. Every student needs a teacher.
- `rc/render.py`      print PDF: per teacher a cover page (prize tally + bagging checklist), then that
  class's certificates (drawn by `make_certificates.draw_certificate`, unchanged). Letter landscape.
- `rc/import_master.py` one-time: master spreadsheet printed to PDF -> first checkpoint (name, teacher, grade,
  previous total). The prize "x" columns are deliberately ignored. `--teacher-alias OLD=NEW` merges classes.
- `rc/workspace.py`   data folder (outside git, e.g. iCloud Drive): `<season>/<month>/{scans,work,
  checkpoint.xlsx,certificates.pdf,summary.txt}`. **Totals reset every school year (July-June)**; a new
  season starts with no checkpoint, and last season's final one is used only as name/teacher hints.
- `rc/matching.py`    sheet name + teacher -> student: exact / likely / ambiguous / hint / none.
- `rc/extract.py`     reads scans with Claude vision (`claude-opus-5-5`) into `work/records.json`; every raw answer is
  cached in `work/raw/` so re-runs are free. Run as `op run --env-file=.env -- python -m rc --data D extract --month M`
  (`.env` holds only a 1Password reference). First measured cost: about $0.28 per 10 pages.
- `rc/review_web.py` + `rc/review.html`  `python -m rc --data D review --month M`: local page (127.0.0.1, token-guarded) with the scan
  beside each question; saves to `work/decisions.json` as you go. One sheet at a time; every open sheet has a pre-filled
  suggestion (`review.suggest`) so the operator mostly presses Enter, or "Accept all suggestions". The month written
  on a sheet never blocks: the batch month is used and a mismatch is only shown (early next-month dates count). Nicknames: built-in groups (Rozzie/Rosalind) plus
  per-child "Also called" names remembered from review. Page text is only ever set with textContent (handwriting is untrusted).
- `rc/review.py`      turns extracted sheet records + a person's decisions into entries; nothing is settled
  silently unless the name matches exactly and every field was read confidently. A student with no
  teacher must have one looked up by the operator (no automated source): `decide <sheet> --teacher X`.
- Tests use made-up children only. Run with `.venv/bin/python -m pytest -q` (Python via `uv`, see below).
- Python: the system `python3` shim is broken on this Mac; use `uv venv .venv && uv pip install -r requirements.txt`.
