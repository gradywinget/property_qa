"""The two kinds of notes the pipeline attaches to a record.

Fix      - something we changed, and are certain didn't change the meaning
           ("$1,250,000" -> 1250000.00). Clean records carry their fixes so
           anyone can audit what the pipeline did.
Problem  - a reason the record cannot be trusted. Any Problem sends the
           record to quarantine, with the original raw value attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Fix:
    field: str
    action: str  # short machine-readable code, e.g. "removed_currency_formatting"
    before: Any
    after: Any


@dataclass(frozen=True)
class Problem:
    field: str  # schema field name, or "__row__" / "__record__" for whole-row issues
    code: str  # short machine-readable code, e.g. "upstream_error_code"
    message: str  # human-readable explanation
    raw_value: Any = None  # the value exactly as it arrived
