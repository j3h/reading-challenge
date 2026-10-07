# Reading Challenge Certificates

Generates the monthly Beverly Cleary Reading Challenge certificates (letter landscape PDF)
from a CSV of children's page counts. Replaces a Canva "bulk create" workflow; works offline.

## Commands
- Setup: `pip install -r requirements.txt`
- Generate: `python make_certificates.py kids.csv --month "October 2026"` -> `out/` (one PDF per
  child, `all_certificates.pdf` for printing, `prizes.csv` listing prizes owed)
- Try it: `python make_certificates.py sample.csv --month "October 2026"`
- Test: `python -m pytest -q`
- New Canva design: `python tools/make_blank_background.py export.pdf` (rewrites
  `background_blank.pdf` and prints text positions/sizes/colours to copy into the constants)

## Layout
- `make_certificates.py`  everything: CSV parsing, level logic, typesetting, output
- `levels.csv`            prize chart: threshold, label, prize (edit this, not the code)
- `background_blank.pdf`  Canva export with all text removed (stripes, books, borders only)
- `fonts/`                Lexend Deca Regular (static instance) and Leckerli One, both OFL
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
