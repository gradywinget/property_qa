"""Stage 1 of 3: PARSE. Turn a file into raw rows, nothing more.

Real parsers (csv, json) do the structural work; regex is never used to split
a file, because quoted commas, escaped quotes and nested objects break it.

Each row remembers where it came from (file + line or record number), so any
rejection can be traced back to the exact spot in the source file.

Two levels of failure:
  - A bad ROW (wrong number of CSV fields, one broken JSONL line) becomes a
    row with a structural Problem. The rest of the file still gets processed.
  - A bad FILE (not UTF-8, invalid JSON document, empty) raises FileUnreadable.
    The pipeline records it and holds the batch, because "some of the data
    silently didn't arrive" is worse than "nothing arrived".
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .issues import Problem


class FileUnreadable(Exception):
    """The whole file is unusable; no rows from it can be trusted."""


@dataclass
class RawRow:
    source: dict[str, Any]  # {"file": "x.csv", "line": 12} or {"file": "x.json", "record": 3}
    data: dict[str, Any]
    problems: list[Problem] = field(default_factory=list)  # structural, found while parsing


def _decode(path: Path) -> str:
    raw = path.read_bytes()
    if not raw.strip():
        raise FileUnreadable("file is empty")
    try:
        # utf-8-sig silently strips the byte-order mark Excel puts at the start of CSVs
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        # Deliberately no guessing at other encodings: a wrong guess turns
        # "Muñoz" into "MuÃ±oz" with no error. Ask the sender for UTF-8 instead.
        raise FileUnreadable(f"not valid UTF-8 (first bad byte at offset {e.start}); re-export as UTF-8") from e


def read_csv(path: Path) -> Iterator[RawRow]:
    reader = csv.reader(io.StringIO(_decode(path), newline=""))
    try:
        header = next(reader)
        if not any(h.strip() for h in header):
            raise FileUnreadable("header row is blank")
        if len(set(header)) != len(header):
            raise FileUnreadable(f"duplicate column names in header: {header}")
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue  # blank line
            source = {"file": path.name, "line": reader.line_num}
            if len(row) != len(header):
                # Fields are misaligned, so we can't know which value belongs to which column.
                yield RawRow(source, {"_fields": row}, [Problem(
                    "__row__", "wrong_field_count",
                    f"row has {len(row)} fields but the header has {len(header)}", row,
                )])
                continue
            yield RawRow(source, dict(zip(header, row)))
    except StopIteration:
        raise FileUnreadable("file has no header row") from None
    except csv.Error as e:
        raise FileUnreadable(f"CSV is malformed near line {reader.line_num}: {e}") from e


def _object_row(source: dict[str, Any], value: Any) -> RawRow:
    if isinstance(value, dict):
        return RawRow(source, value)
    return RawRow(source, {"_value": value}, [Problem(
        "__row__", "not_an_object", f"expected a JSON object, got {type(value).__name__}", value,
    )])


def read_json(path: Path) -> Iterator[RawRow]:
    text = _decode(path)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise FileUnreadable(f"invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from e
    # Accept a bare array, or the common {"records": [...]} wrapper.
    if isinstance(doc, dict) and isinstance(doc.get("records"), list):
        doc = doc["records"]
    if not isinstance(doc, list):
        raise FileUnreadable('expected a JSON array of records, or an object with a "records" array')
    for i, item in enumerate(doc, start=1):
        yield _object_row({"file": path.name, "record": i}, item)


def read_jsonl(path: Path) -> Iterator[RawRow]:
    # JSON Lines: one JSON object per line. A broken line only costs that line.
    for lineno, line in enumerate(_decode(path).splitlines(), start=1):
        if not line.strip():
            continue
        source = {"file": path.name, "line": lineno}
        try:
            value = json.loads(line)
        except json.JSONDecodeError as e:
            yield RawRow(source, {"_line": line}, [Problem("__row__", "invalid_json", f"line is not valid JSON: {e.msg}", line)])
            continue
        yield _object_row(source, value)


READERS = {".csv": read_csv, ".json": read_json, ".jsonl": read_jsonl}


def read_rows(path: Path) -> Iterator[RawRow]:
    return READERS[path.suffix.lower()](path)
