"""Command line:  python -m rc <command> --data <folder> --month "October 2026" ...

  start    create the month's folders and say where to put the scans
  check    list every open question about the scanned sheets (exit status 1 if any)
  decide   answer a question from the command line (until the review page exists)
  finish   write certificates.pdf, summary.txt and checkpoint.xlsx for the month
  import-master  one-time: master spreadsheet PDF -> starting checkpoint for a season

The data folder can also come from the READING_CHALLENGE_DATA environment variable.
"""
import argparse
import os
import sys

from rc import checkpoint as ck
from rc import import_master
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


def cmd_decide(args):
    m = _month(args)
    d = {}
    if args.student:
        d["student"] = args.student
    if args.new_name or args.teacher:
        d["new_student"] = {"name": args.new_name or "", "teacher": args.teacher or "", "grade": args.grade or ""}
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
