"""Command line:  uv run python -m property_qa data/raw --out output

Exit code 0 = clean data published, 1 = batch held for a human to look at.
Schedulers (cron, Airflow, GitHub Actions) use the exit code to decide
whether to load the data or raise an alert.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import DEFAULT_MAX_REJECT_RATE, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="property_qa", description="Validate messy property-record files.")
    parser.add_argument("input_dir", type=Path, help="folder of .csv / .json / .jsonl files")
    parser.add_argument("--out", type=Path, default=Path("output"), help="output folder (default: output)")
    parser.add_argument(
        "--max-reject-rate", type=float, default=DEFAULT_MAX_REJECT_RATE,
        help=f"hold the batch if more than this share of rows is rejected (default: {DEFAULT_MAX_REJECT_RATE})",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.max_reject_rate <= 1:
        parser.error("--max-reject-rate must be between 0 and 1")

    report = run(args.input_dir, args.out, args.max_reject_rate)
    t = report["totals"]
    print(f"{report['status'].upper()}: {t['rows_read']} rows read, {t['clean']} clean, "
          f"{t['quarantined']} quarantined, {t['exact_duplicates_dropped']} exact duplicates dropped "
          f"({t['reject_rate']:.0%} rejected)")
    for reason in report["hold_reasons"]:
        print(f"  HOLD: {reason}")
    for f in report["files"]:
        if f["error"]:
            print(f"  {f['status']}: {f['file']}: {f['error']}")
    print("Top rejection reasons:")
    for reason, count in list(report["reject_reasons"].items())[:8]:
        print(f"  {count:>3}  {reason}")
    print(f"Outputs in {args.out}/ (report.json has the full breakdown)")
    return 0 if report["status"] == "published" else 1


if __name__ == "__main__":
    sys.exit(main())
