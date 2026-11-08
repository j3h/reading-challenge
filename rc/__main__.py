"""Command line:  python -m rc <command> --data <folder> --month "October 2026" ...

  start    create the month's folders and say where to put the scans
  check    list every open question about the scanned sheets (exit status 1 if any)
  extract  read the scanned sheets with Claude (run as: op run --env-file=.env -- python -m rc ... extract)
  review   open the review screen in your browser (scan beside each question)
  decide   answer a question from the command line
  finish   write certificates.pdf, summary.txt and checkpoint.xlsx for the month
  import-master  one-time: master spreadsheet PDF -> starting checkpoint for a season

The data folder can also come from the READING_CHALLENGE_DATA environment variable.
"""
import argparse
import os
import sys

from rc import checkpoint as ck
from rc import extract
from rc import import_master
from rc import review_web
from rc import runner
from rc import workspace as wsp


def _data(args):
    path = args.data or os.environ.get("READING_CHALLENGE_DATA")
    if not path:
        sys.exit("Say where the data folder is: --data <folder> or set READING_CHALLENGE_DATA")
    return wsp.Workspace(path)


def _month(args):
    return runner.Month(_data(args), args.month)


def cmd_start(args):
    m = _month(args)
    m.folder.create()
    prev, why = m.previous()
    print(f"{ck.month_label(m.folder.month)}: {why} ({len(prev.students)} students)")
    print(f"Put the scans in: {m.folder.scans}")
    stray = wsp.cloud_placeholders(m.folder.path)
    if stray:
        print(f"Warning: {len(stray)} iCloud file(s) are not downloaded yet; open the folder in Finder first.")


def cmd_check(args):
    p = _month(args).plan()
    open_ = runner.describe_issues(p)
    skipped = sum(1 for s in p.settled if s.skipped)
    print(f"{len(p.settled)} sheets, {skipped} skipped, {len(p.issues)} open question(s)")
    for line in open_:
        print(line)
    sys.exit(1 if p.issues else 0)


def _is_new_student(m, args, current):
    """Does this --teacher/--grade belong to a student being added, or to someone already on the roster?"""
    if args.new_name or args.grade or "new_student" in current:
        return True
    if args.student or "student" in current or not args.teacher:
        return False
    match = m.plan().matches.get(args.sheet)         # a bare --teacher: for whoever the sheet matched
    return match is None or match.status != "exact"


def cmd_extract(args):
    m = _month(args)
    if args.dry_run:
        count, total, largest, done = extract.dry_run(m.folder, args.pages, args.dpi)
        print(f"{count} page(s) would be read at {args.dpi} dpi; images total {total / 1e6:.1f} MB "
              f"(largest {largest / 1e6:.2f} MB); {done} already read and kept. No API calls made.")
        return
    try:
        client = extract.make_client()
        records, dropped, usage = extract.extract_month(client, m.folder, args.model, args.dpi, args.pages,
                                                        args.force, args.workers)
    except extract.ExtractionError as e:
        sys.exit(f"error: {e}")
    print(f"\nRead {len(records)} sheet(s) -> {m.records_path}")
    if dropped:
        print(f"Not reading sheets (blank or other pages), left out: {', '.join(dropped)}")
    print("Cost this run:", extract.cost_estimate(usage, args.model))
    print('Next: python -m rc --data <folder> check --month "%s"' % ck.month_label(m.folder.month))


def cmd_review(args):
    review_web.serve(_month(args), open_browser=not args.no_browser, port=args.port)


def cmd_decide(args):
    m = _month(args)
    d = {}
    if args.student:
        d["student"] = args.student
    current = m.decisions().get(args.sheet, {})
    if _is_new_student(m, args, current):
        merged = dict(current.get("new_student", {}))
        for key, value in (("name", args.new_name), ("teacher", args.teacher), ("grade", args.grade)):
            if value:
                merged[key] = value
        d["new_student"] = merged
    elif args.teacher:
        d["teacher"] = args.teacher
    if args.skip:
        d["skip"] = args.skip
    if args.pages is not None:
        d["pages"] = args.pages
    if args.pages_ok:
        d["pages_ok"] = True
    if args.month_ok:
        d["month_ok"] = True
    if args.duplicate:
        d["duplicate"] = args.duplicate
    if not d:
        sys.exit("Nothing to record: give --student, --new-name/--teacher, --skip, --pages, ...")
    m.decide(args.sheet, **d)
    print(f"Recorded for {args.sheet}: {d}")


def cmd_finish(args):
    m = _month(args)
    text = m.finish()
    print(text)
    print(f"Wrote {m.folder.certificates}\n      {m.folder.summary}\n      {m.folder.checkpoint}")


def cmd_import(args):
    wsp_ = _data(args)
    season = args.season
    out = wsp_.root / season / "start" / wsp.CHECKPOINT
    if out.exists() and not args.replace:
        sys.exit(f"{out} already exists; use --replace to overwrite it")
    extra = (["--questions", args.questions] if args.questions else [])
    for a in args.teacher_alias:
        extra += ["--teacher-alias", a]
    import_master.main([args.pdf, "--out", str(out)] + extra)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m rc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", help="data folder (outside git; e.g. in iCloud Drive)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("start", cmd_start), ("check", cmd_check), ("finish", cmd_finish)):
        p = sub.add_parser(name)
        p.add_argument("--month", required=True, help='e.g. "October 2026"')
        p.set_defaults(fn=fn)
    p = sub.add_parser("extract", help="read the scanned sheets with Claude (needs the API key: see rc/extract.py)")
    p.add_argument("--month", required=True)
    p.add_argument("--pages", help="only these scan pages, e.g. 1-10 (a trial slice)")
    p.add_argument("--model", default=extract.DEFAULT_MODEL)
    p.add_argument("--dpi", type=int, default=extract.DEFAULT_DPI)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--force", action="store_true", help="ask again even for pages already read")
    p.add_argument("--dry-run", action="store_true", help="render the pages and report sizes; no API calls")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("review", help="open the review screen in your browser")
    p.add_argument("--month", required=True)
    p.add_argument("--port", type=int, default=0, help="fixed port (default: any free one)")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("decide")
    p.add_argument("--month", required=True)
    p.add_argument("sheet", help='sheet key as shown by check, e.g. "oct.pdf#12"')
    p.add_argument("--student", help="existing student id, e.g. S0003")
    p.add_argument("--new-name", help="add a new student with this name")
    p.add_argument("--teacher", help="teacher for the new student")
    p.add_argument("--grade", help="grade for the new student")
    p.add_argument("--skip", metavar="REASON", help="ignore this sheet")
    p.add_argument("--pages", type=int, help="use this page count")
    p.add_argument("--pages-ok", action="store_true", help="the page count that was read is right")
    p.add_argument("--month-ok", action="store_true", help="count it although the sheet shows another month")
    p.add_argument("--duplicate", choices=["sum", "skip"])
    p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("import-master")
    p.add_argument("pdf")
    p.add_argument("--season", required=True, help='e.g. "2026-27"')
    p.add_argument("--questions", help="also write a plain-text list of questions about the master")
    p.add_argument("--teacher-alias", action="append", default=[], metavar="OLD=NEW",
                   help='use NEW wherever the master says OLD, e.g. "Biello/Tanner=Biello" (repeatable)')
    p.add_argument("--replace", action="store_true")
    p.set_defaults(fn=cmd_import)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except ck.CheckpointError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
