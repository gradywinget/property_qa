"""Runs the stages over a folder of files and writes three outputs:

  output/clean.jsonl       records that passed every check (one JSON object per line)
  output/quarantine.jsonl  records that didn't, each with the raw input and every reason
  output/report.json       what happened: counts, top rejection reasons, fixes applied

Safety rules:
  * Every input row ends up in exactly one place: clean, quarantine, or
    counted as an exact duplicate. Nothing disappears silently.
  * The batch is HELD (clean.jsonl not published) if a whole file was
    unreadable, if there were no rows at all, or if the share of rejected rows
    exceeds max_reject_rate. A sudden spike in rejections usually means the
    source changed its format, and that should page a human, not trickle bad
    assumptions into the database.
  * Files are written to a temporary name and then renamed. A rename is atomic,
    so a crash mid-run can never leave a half-written clean.jsonl for the
    next system to read.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .issues import Fix, Problem
from .normalize import AMBIGUOUS_ORDER_ACTION, Normalized, normalize_row
from .parse import READERS, FileUnreadable, RawRow, read_rows
from .schema import REQUIRED_FIELDS, SCHEMA_VERSION, PropertyRecord

DEFAULT_MAX_REJECT_RATE = 0.5


def _dumps(obj: Any) -> str:
    # default=str turns Decimal("1250000.00") into "1250000.00" and dates into "2024-03-15"
    return json.dumps(obj, default=str, ensure_ascii=False)


def validate(n: Normalized) -> tuple[PropertyRecord | None, list[Problem]]:
    """Check normalized values against the schema. Collects ALL problems, not just the first."""
    problems = list(n.problems)
    bad = {p.field for p in problems}

    for name in REQUIRED_FIELDS:
        if name not in bad and n.values.get(name) is None:
            problems.append(Problem(name, "missing_required", "required field is missing or empty", n.raw_by_field.get(name)))
            bad.add(name)

    candidate = {k: v for k, v in n.values.items() if k not in bad}
    try:
        record = PropertyRecord.model_validate(candidate)
    except ValidationError as e:
        for err in e.errors():
            name = str(err["loc"][0]) if err["loc"] else "__record__"
            if name in bad and err["type"] == "missing":
                continue  # already explained by an earlier, clearer problem
            problems.append(Problem(name, err["type"], err["msg"], n.raw_by_field.get(name)))
        return None, problems
    return (record, []) if not problems else (None, problems)


@dataclass
class _Accepted:
    record: PropertyRecord
    source: dict[str, Any]
    raw: dict[str, Any]  # kept in case a later duplicate check sends this row to quarantine
    fixes: list[Fix]


@dataclass
class _FileStats:
    file: str
    status: str = "processed"
    rows: int = 0
    error: str | None = None


@dataclass
class _Run:
    accepted: dict[str, list[_Accepted]] = field(default_factory=dict)  # property_id -> rows
    quarantine_lines: list[str] = field(default_factory=list)
    reject_reasons: Counter = field(default_factory=Counter)
    fix_counts: Counter = field(default_factory=Counter)
    day_first_examples: dict[str, str] = field(default_factory=dict)  # file -> a date proving day-first order

    def quarantine(self, source: dict, raw: Any, problems: list[Problem], fixes: list[Fix]) -> None:
        for p in problems:
            if p.code == "day_first_date":
                self.day_first_examples.setdefault(source["file"], p.raw_value)
        self.quarantine_lines.append(_dumps({
            "source": source,
            "raw": raw,
            "problems": [asdict(p) for p in problems],
            "fixes_attempted": [asdict(f) for f in fixes],
        }))
        for p in problems:
            self.reject_reasons[f"{p.field}: {p.code}"] += 1

    def process(self, row: RawRow) -> None:
        if row.problems:  # structural: the row's fields can't be trusted at all
            self.quarantine(row.source, row.data, row.problems, [])
            return
        n = normalize_row(row.data)
        record, problems = validate(n)
        if record is None:
            self.quarantine(row.source, row.data, problems, n.fixes)
            return
        self.accepted.setdefault(record.property_id, []).append(_Accepted(record, row.source, row.data, n.fixes))


def _recheck_date_order(run: _Run) -> None:
    """A date like 05/02/2024 was read month-first (May 2). If the SAME file also
    contains a date like 13/05/2024, which can only be day-first, the file mixes
    orders or is day-first throughout, and May 2 is no longer a safe reading.
    Those rows move to quarantine. This is what stops a source that switches to
    DD/MM from silently swapping days and months on the 1st-12th of each month."""
    if not run.day_first_examples:
        return
    for pid in list(run.accepted):
        keep = []
        for r in run.accepted[pid]:
            example = run.day_first_examples.get(r.source["file"])
            if example and any(f.field == "purchase_date" and f.action == AMBIGUOUS_ORDER_ACTION for f in r.fixes):
                raw_date = next(f.before for f in r.fixes if f.field == "purchase_date")
                run.quarantine(r.source, r.raw, [Problem(
                    "purchase_date", "inconsistent_date_order",
                    f"{raw_date!r} could be month-first or day-first, and this file also contains "
                    f"{example!r}, which can only be day-first",
                    raw_date,
                )], r.fixes)
            else:
                keep.append(r)
        if keep:
            run.accepted[pid] = keep
        else:
            del run.accepted[pid]


def _resolve_duplicates(run: _Run) -> tuple[list[str], int]:
    """Same property_id more than once. Identical copies: keep one. Conflicting copies:
    we can't know which is right, so ALL of them go to quarantine, including the first."""
    clean_lines, exact_dupes = [], 0
    for pid, rows in run.accepted.items():
        if len({r.record for r in rows}) > 1:  # frozen models compare and hash by value
            where = ", ".join(_where(r.source) for r in rows)
            for r in rows:
                run.quarantine(r.source, r.raw, [Problem(
                    "property_id", "conflicting_duplicate",
                    f"{pid} appears {len(rows)} times with different data ({where}); can't tell which is correct",
                    pid,
                )], r.fixes)
            continue
        exact_dupes += len(rows) - 1
        first = rows[0]
        for f in first.fixes:
            run.fix_counts[f"{f.field}: {f.action}"] += 1
        clean_lines.append(_dumps({
            **first.record.model_dump(mode="json"),
            "_meta": {"source": first.source, "fixes": [asdict(f) for f in first.fixes], "schema_version": SCHEMA_VERSION},
        }))
    return clean_lines, exact_dupes


def _where(source: dict) -> str:
    pos = f"line {source['line']}" if "line" in source else f"record {source['record']}"
    return f"{source['file']} {pos}"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on the same filesystem


def run(input_dir: Path, output_dir: Path, max_reject_rate: float = DEFAULT_MAX_REJECT_RATE) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    if not input_dir.is_dir():
        raise NotADirectoryError(f"input folder not found: {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    state = _Run()
    files: list[_FileStats] = []
    hold_reasons: list[str] = []
    total_rows = 0

    for path in sorted(input_dir.iterdir()):  # sorted = same input always gives the same output
        if path.name.startswith(".") or not path.is_file():
            continue
        stats = _FileStats(path.name)
        files.append(stats)
        if path.suffix.lower() not in READERS:
            stats.status, stats.error = "skipped", f"unsupported file type {path.suffix or '(none)'}"
            continue
        try:
            for row in read_rows(path):
                stats.rows += 1
                total_rows += 1
                state.process(row)
        except FileUnreadable as e:
            stats.status, stats.error = "failed", str(e)
            hold_reasons.append(f"{path.name} could not be read: {e}")

    _recheck_date_order(state)
    clean_lines, exact_dupes = _resolve_duplicates(state)
    quarantined = len(state.quarantine_lines)
    reject_rate = quarantined / total_rows if total_rows else 1.0

    if total_rows == 0:
        hold_reasons.append("no rows found in any input file")
    elif reject_rate > max_reject_rate:
        hold_reasons.append(
            f"{reject_rate:.0%} of rows were rejected, above the {max_reject_rate:.0%} limit; "
            "the source format may have changed"
        )
    status = "held" if hold_reasons else "published"

    clean_path = output_dir / "clean.jsonl"
    if status == "published":
        _atomic_write(clean_path, "".join(line + "\n" for line in clean_lines))
    else:
        # Remove the previous run's clean file too: leaving it would let a downstream
        # job load stale data and believe it came from this run.
        clean_path.unlink(missing_ok=True)
    _atomic_write(output_dir / "quarantine.jsonl", "".join(line + "\n" for line in state.quarantine_lines))

    report = {
        "status": status,
        "hold_reasons": hold_reasons,
        "schema_version": SCHEMA_VERSION,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_dir": str(input_dir),
        "max_reject_rate": max_reject_rate,
        "totals": {
            "rows_read": total_rows,
            "clean": len(clean_lines),
            "quarantined": quarantined,
            "exact_duplicates_dropped": exact_dupes,
            "reject_rate": round(reject_rate, 4),
        },
        "files": [asdict(f) for f in files],
        "reject_reasons": dict(state.reject_reasons.most_common()),
        "fixes_applied": dict(state.fix_counts.most_common()),
    }
    # Written last: if report.json is fresh, the run finished.
    _atomic_write(output_dir / "report.json", json.dumps(report, indent=2) + "\n")
    return report
