# Reading Challenge Certificates

Monthly certificates for the Beverly Cleary Reading Challenge, generated from a CSV.

```
pip install -r requirements.txt
python make_certificates.py kids.csv --month "October 2026"
```

`kids.csv` needs three columns (a `month` column per row is optional):

```
name,pages,total_pages
Juniper Albright,"3,194","5,043"
```

`pages` is this month's reading and `total_pages` is the running total including this month.
Prize levels are worked out from those numbers using `levels.csv`.

Output goes to `out/`: one PDF per child, `all_certificates.pdf` for printing, and `prizes.csv`
listing the prizes each child has earned this month.

See `CLAUDE.md` for how the layout and level logic work, and for changing the design.
