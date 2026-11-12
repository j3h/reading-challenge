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
- `rc/render.py`      one print file, `certificates.pdf` (`render_print_file`): the class lists first (all classes'
  bagging lists in two columns on as few pages as possible, school-wide prize count, owed prizes marked), then the
  certificates 2 per letter sheet, cut-and-stack per class (`cut_and_stack`: one cut, bottom pile under top = list
  order), margin label "Teacher · k of n" in the trim area. Certificates drawn by `make_certificates.draw_certificate`,
  unchanged. `reprint --month M` redraws it from the log.
- `rc/import_workbook.py` preferred one-time import: the master WORKBOOK (.xlsx). Totals = end of `--through`
  month from the month tabs (PRIMARY's running total can already include the next month, or miss one); chained rows
  = several sheets; stops at the "Prize | Level" stock table (a "Teacher" in the teacher column is a staff reader,
  not a header). `--keep-ids` reuses IDs so review decisions stay valid. Every disagreement is noted per student.
- `rc/import_master.py` one-time: master spreadsheet printed to PDF -> first checkpoint (name, teacher, grade,
  previous total). The prize "x" columns are deliberately ignored; red rows (no recent sheets) are only noted. `--teacher-alias OLD=NEW` merges classes.
- Data folder top level: `events/`, one folder per school year, and `Imported - evidence only, do not edit/` holding
  the workbook the opening events point at (`master.xlsx`, never updated, name kept so sources still match).
- `rc/workspace.py`   data folder (outside git, e.g. iCloud Drive): `<school year>/<month>/{scans,work,
  checkpoint.xlsx,certificates.pdf,summary.txt}`. **Totals never reset**: a month starts from the latest finished
  month in any school-year folder (year folders only keep things tidy), else the latest `<year>/start/` checkpoint.
- `rc/matching.py`    sheet name + teacher -> student: exact / likely / ambiguous / none. Built-in nickname
  groups are global but never more than "likely"; names remembered in review ("Also called") count only when the
  sheet's teacher is the child's own teacher.
- `rc/extract.py`     reads scans with Claude vision (`claude-opus-5-5`) into `work/records.json`; every raw answer is
  cached in `work/raw/` so re-runs are free. Run as `op run --env-file=.env -- python -m rc --data D extract --month M`
  (`.env` holds only a 1Password reference). First measured cost: about $0.28 per 10 pages.
- `rc/review_web.py` + `rc/review_table.html`  `python -m rc --data D review --month M`: local page (127.0.0.1, token-guarded) with the scan
  beside each question; saves to `work/decisions.json` as you go. A table, one row per sheet; the open row edits each value in its column with the handwriting
  snippet beneath it. Every open sheet has a pre-filled
  suggestion (`review.suggest`) so the operator mostly presses Enter, or "Accept all suggestions". The month written
  on a sheet never blocks: the batch month is used and a mismatch is only shown (early next-month dates count). Nicknames: built-in groups (Rozzie/Rosalind) plus
  per-child "Also called" names remembered from review. Page text is only ever set with textContent (handwriting is untrusted).
- `rc/review.py`      several sheets per child: taken in page order, each later one is add / replace / skip
  (never "Accept all"); a child added as new is matchable from other sheets as `new@<sheet key>`.
- `rc/review.py`      turns extracted sheet records + a person's decisions into entries; nothing is settled
  silently unless the name matches exactly and every field was read confidently. A student with no
  teacher must have one looked up by the operator (no automated source): `decide <sheet> --teacher X`.
- Tests use made-up children only. Run with `.venv/bin/python -m pytest -q` (Python via `uv`, see below).
- Python: the system `python3` shim is broken on this Mac; use `uv venv .venv && uv pip install -r requirements.txt`.

## Inbox and rounds (`runner.Pool`; how a data folder with an event log works now)
- `Scans/` (every file as it arrived, never moved; uploads get a `YYYY-MM-DD ` prefix, Finder drop-ins are fine)
  and `App working files (do not edit)/` (records.json, decisions.json, pool.json, files.json, raw/) instead of
  month folders; `Inbox.txt` is written for people (`Pool.write_inbox_list`, only when it changes) and never read.
  `runner.to_scans_layout` converts the first inbox layout (Inbox/scans, Inbox/work, '<round>/scans') once. A sheet is
  open / ready / held (`{"hold": true}`) / filed. A round (`Pool.finish`) prints the ready ones for the round month
  (`pool.json` `round_month`, default: last calendar month, never older than the latest round) into
  `<season>/<YYYY-MM> round <n>/`; its `month_finished` event carries `round` and `keys` (sheets it used; replay ->
  `State.filed`, `State.rounds`). `round_plan` holds back open sheets and every other sheet of their child.
- A sheet for a child who already has pages for the round month, or for the month the sheet names, is a `repeat`
  question (hold / skip / accept = `repeat_ok`); Hold in the UI also sets `repeat_ok`. Accepted for a month the child
  already has pages for: `runner.reprint_map` -> the round prints their certificate again with the whole month (pages
  and every level reached that month; margin "REPLACES EARLIER"); the class list says "reprint" and bags only the new
  prizes (events record only this round's pages and new prizes).
- Form CSVs: `forms.pool_records` (month answer resolved against the submit date; rows before `forms_from` ignored).
  Keys are `file#row`, but an answer is identified by `forms.row_digest` (SHA-256 of its cells): a re-download
  replaces the file, decisions follow answers to their new rows (`Home._follow_answers`), and answers filed before
  digests existed get a `sheets_filed` with `digests` in their round's batch first. `Pool.is_filed`: forms by digest
  (by key only if no digest was ever recorded for that key), scans by key.
- Fingerprints: `wsp.FileRegistry` (`work/files.json`, SHA-256 of every file under scans/ incl. originals/, as first
  seen; rehash only when size/mtime change). Same bytes under a new name are refused on upload; a scan whose content
  changed is a problem that blocks rounds and extraction. `SheetRecord.digest` = file SHA-256 (scans/photos) or row
  digest (forms); event sources carry it as `sha256` / `row_sha256`; a round's `month_finished` has `digests`
  aligned with `keys`. The digest is of the stored file: downscaling for the API (`extract.page_pixmap`, max 2000 px)
  happens in memory only.
- `Pool.undo_round` (latest only) voids its batch and renames the folder "(undone …)". `move_to_inbox` / `to-inbox` /
  the app's "Move them" card: one-time migration; a finished month's files become round 1 and a `sheets_filed` event
  in its finish batch marks its sheets filed. Month folders (`runner.Month`) remain for folders without a log.
- Prizes count as given when a round prints (`prizes_given`); "Not handed out" on a child's page voids that event,
  so the prize is owed on the next round's list. Let-go of old raw scans is month-folder only (not in the inbox yet).

## Event log (`rc/events.py`)
Every change is an append-only event with who/when/which Mac, a reason, and `sources` pointing at the evidence
(scan page, photo, form row, workbook tab+row, or manual). State = replay in time order; checkpoint.xlsx is a
snapshot (`python -m rc snapshot OUT`). One file per Mac in `<data>/events/<device>.jsonl` (temp file + rename),
merged on read, so iCloud never sees two versions of one file. Duplicates: same id always; same content only for
additive events tied to a document (manual corrections and set-a-value events are never merged by content).
Fix mistakes with new events (`adjust --reason`, `--void ID`), never by editing. `history NAME` shows a child's
timeline. `import-master x.xlsx` writes the opening events (history back to 2021, `total_set` where tabs do not add
up, prizes up to the opening total recorded as handed out, so `State.owed()` lists only new ones).
In a folder with a log, a month starts from the replay (`Month.previous`) and `finish(who=...)` appends one batch
(month_finished, new students, teacher changes, also-called names, pages_recorded and prizes_given, each pointing at
its sheet) after checking that the replay gives the same totals; `reopen` voids that batch. Folders without a log
still use checkpoint files. Google Form responses: the CSV download goes in a month's scans folder; `rc/forms.py`
turns that month's rows (by the "Month" answer) into SheetRecords (file#row, grade, submitted) that join review like
any sheet (`Month.records`), with source {"form", "row", "submitted"}. The app's Students page (`Home.students/student/
adjust`) lists children, shows each one's timeline with sources and cancellations, and records corrections (reason
required) via `events.adjustment_events`. Prizes owed (crossed, never given) are bagged at the next finish: on the
cover pages marked "(owed)", in the summary, and recorded as `prizes_given`. Teacher grades: `teacher_grade_set` events (`State.teacher_grades`); a new student with no
grade takes the teacher's, a class change in review adds a `grade_set` to the new class's grade; the app's Teachers page
sets them and can align a class. A child's page has a by-month table (`Home.student()["months"]`: pages, total, prizes,
sheet links while the scan is kept, `jump` when a total was set in between). Raw data: `Home.prunable/let_go` offer (never automatic) to move scans, photos, form CSVs
and cached readings of finished months older than `keep_raw_months` (Settings, default 6) to the Trash; the month
keeps its outputs, records.json and decisions, and notes the removal in `work/removed.txt`.

## The app (`python -m rc app`, double-click "Reading Challenge" on a set-up Mac)
For a non-programmer (Jamie): the whole month in the browser, no CLI. See `docs/mac-setup.md` (install) and
`docs/how-to.md` (using it).
- `rc/app.py` `Home`: month list and status, create month (refused while an earlier month is unfinished), add
  scans (upload, written whole then renamed), read in a background thread with progress, reopen (outputs moved
  to `<month>/reopened <time>/`; refused if a later month is finished). No HTTP in it.
- `rc/home.html` home screen; `review_web.make_server(home=...)` serves it at `/` and each month's review at
  `/review?m=YYYY-MM` (every API call carries `m`). `/ping` is for the launcher.
- `rc/settings.py` data folder in `~/Library/Application Support/Reading Challenge/settings.json`; API key in the
  Keychain via `keyring` (an env key, e.g. from `op run`, wins). Tests use a fake keyring: never the real one.
- `tools/launch.sh` (port 8642, reuses a running copy, `git pull --ff-only` when possible), `tools/setup_mac.sh`.
