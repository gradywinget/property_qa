"""Stage 2 of 3: NORMALIZE. Safe, logged, format-only fixes.

The one rule: change how a value is WRITTEN, never WHAT it is.

  "$1,250,000"  -> 1250000.00     same number, different notation   -> fix
  "California"  -> "CA"           same state                        -> fix
  "03/15/2024"  -> 2024-03-15     same day                          -> fix
  "CRITICAL_TIMESTAMP_PARSE_ERROR" -> ???  the real date is unknown  -> Problem
  "3/4/24"      -> 1924 or 2024?  ambiguous                         -> Problem
  "13/05/2024"  -> May 13?        not valid US format, would be a guess -> Problem

When a value can't be read with certainty, we raise Unparseable and the record
goes to quarantine with the raw value attached. We never fill in a default,
because a made-up value looks exactly like a real one downstream.

Normalizing never rejects for business reasons (negative price, future date);
that's the schema's job. Keeping "can I read it?" separate from "is it allowed?"
makes both easier to test.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .issues import Fix, Problem
from .schema import ALL_FIELDS, US_STATES


class Unparseable(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Normalized:
    values: dict[str, Any] = field(default_factory=dict)
    raw_by_field: dict[str, Any] = field(default_factory=dict)
    fixes: list[Fix] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


# ---------------------------------------------------------------- column names

# Different sources call the same thing different names.
COLUMN_ALIASES = {
    "parcel_id": "property_id", "listing_id": "property_id", "id": "property_id",
    "street_address": "address", "street": "address",
    "st": "state",
    "zip_code": "zip", "zipcode": "zip", "postal_code": "zip",
    "sale_price": "purchase_price", "price": "purchase_price", "sold_price": "purchase_price",
    "sale_date": "purchase_date", "sold_date": "purchase_date", "close_date": "purchase_date",
    "type": "property_type",
    "beds": "bedrooms", "baths": "bathrooms",
    "sq_ft": "sqft", "square_feet": "sqft", "living_area": "sqft",
}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")  # "salePrice" -> "sale|Price"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_column(name: str) -> str:
    """'Sale Price', 'salePrice', 'sale-price ' -> 'sale_price' -> 'purchase_price'"""
    s = _CAMEL_BOUNDARY.sub("_", name.strip())
    s = _NON_ALNUM.sub("_", s.lower()).strip("_")
    return COLUMN_ALIASES.get(s, s)


# ---------------------------------------------------------------- shared pre-step

_INVISIBLE = re.compile(r"[​‌‍﻿]")  # zero-width chars, stray BOMs
_WHITESPACE = re.compile(r"\s+")  # includes tabs, newlines, non-breaking spaces
_NULL_TOKEN = re.compile(r"^(?:n/?a|null|none|nil|nan|unknown|tbd|-+|\?+)?$", re.IGNORECASE)
# Values an upstream system writes when IT crashed: "CRITICAL_TIMESTAMP_PARSE_ERROR",
# "GEOCODER_FAILURE", and Excel's "#VALUE!", "#REF!", "#DIV/0!" ...
_ERROR_CODE = re.compile(
    r"^(?:[A-Z][A-Z0-9]*_)+(?:ERROR|ERR|FAILURE|FAILED|EXCEPTION|TIMEOUT)$"
    r"|^#(?:VALUE!|REF!|DIV/0!|NAME\?|NUM!|NULL!|N/A)$"
)


def _prepare(value: Any) -> tuple[Any, list[str]]:
    """Tidy whitespace, detect error codes and 'missing' tokens. Returns (value, fix actions)."""
    if value is None:
        return None, []
    if isinstance(value, bool):
        raise Unparseable("wrong_type", f"got a true/false value {value!r}")
    if isinstance(value, float) and math.isnan(value):
        return None, []
    if not isinstance(value, str):
        return value, []  # numbers from JSON go straight to the field's normalizer

    actions = []
    cleaned = _WHITESPACE.sub(" ", _INVISIBLE.sub("", value)).strip()
    if cleaned != value:
        actions.append("cleaned_whitespace")
    if _ERROR_CODE.match(cleaned):
        # Even for an optional field: an upstream crash on this row means its
        # other fields are suspect too, so the whole row is quarantined.
        raise Unparseable("upstream_error_code", f"the source system wrote the error code {cleaned!r} instead of a value")
    if _NULL_TOKEN.match(cleaned):
        if cleaned:
            actions.append("null_token_to_missing")
        return None, actions
    return cleaned, actions


# ---------------------------------------------------------------- field normalizers
# Each takes (value, values_normalized_so_far) and returns (new_value, [fix actions]).

Normalizer = Callable[[Any, dict[str, Any]], tuple[Any, list[str]]]


def _text(value: Any, _: dict) -> tuple[Any, list[str]]:
    # No title-casing: it would turn "McAllen" into "Mcallen". Whitespace only.
    if not isinstance(value, str):
        raise Unparseable("wrong_type", f"expected text, got {type(value).__name__}")
    return value, []


def _upper_code(value: Any, _: dict) -> tuple[Any, list[str]]:
    if not isinstance(value, str):
        raise Unparseable("wrong_type", f"expected text, got {type(value).__name__}")
    upper = value.upper()
    return upper, (["uppercased"] if upper != value else [])


_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _state(value: Any, _: dict) -> tuple[Any, list[str]]:
    if not isinstance(value, str):
        raise Unparseable("wrong_type", f"expected text, got {type(value).__name__}")
    if value.upper() in US_STATES:
        return value.upper(), (["uppercased"] if value != value.upper() else [])
    code = _STATE_NAMES.get(value.lower())
    if code:
        return code, ["state_name_to_code"]
    return value, []  # unknown: let the schema reject it with a clear message


# ZIPs that start with 0 belong to these states. Excel drops the leading zero
# ("02134" -> 2134), so a 4-digit ZIP is a known, fixable artifact ONLY here.
# Anywhere else, 4 digits means a digit was lost and we can't know which one.
_LEADING_ZERO_STATES = frozenset({"CT", "MA", "ME", "NH", "NJ", "RI", "VT"})
_ZIP5 = re.compile(r"^(\d{5})(?:-(\d{4}))?$")
_ZIP4 = re.compile(r"^(\d{4})$")
_ZIP_EXCEL_FLOAT = re.compile(r"^(\d{4,5})\.0$")  # 94110 -> "94110.0" after a spreadsheet


def _zip(value: Any, values: dict) -> tuple[Any, list[str]]:
    actions = []
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise Unparseable("wrong_type", f"expected a ZIP code, got {type(value).__name__}")
    if m := _ZIP_EXCEL_FLOAT.match(value):
        value = m.group(1)
        actions.append("removed_spreadsheet_decimal")
    if m := _ZIP5.match(value):
        if m.group(2):
            actions.append("dropped_zip_plus4")
        return m.group(1), actions
    if _ZIP4.match(value):
        state = values.get("state")
        if state in _LEADING_ZERO_STATES:
            return "0" + value, actions + ["restored_leading_zero"]
        raise Unparseable(
            "ambiguous_zip",
            f"{value!r} has 4 digits; a dropped leading zero is only possible for "
            f"New England / NJ addresses, and this one is in {state or 'an unknown state'}",
        )
    raise Unparseable("unreadable_zip", f"could not read {value!r} as a ZIP code")


_PRICE = re.compile(
    r"""^
    (?P<neg1>-)?\s*
    (?:USD\s*|\$\s*)?              # optional currency prefix
    (?P<neg2>-)?\s*
    (?P<whole>\d{1,3}(?:,\d{3})+|\d+)  # 1,250,000 or 1250000 (commas must be in thousands groups)
    (?:\.(?P<frac>\d+))?
    \s*(?P<suffix>[KkMm])?         # 450K, 1.2M
    (?:\s*USD)?                    # optional currency suffix
    $""",
    re.VERBOSE,
)
_ACCOUNTING_NEGATIVE = re.compile(r"^\((.*)\)$")  # (450,000) means -450,000 in accounting


def _price(value: Any, _: dict) -> tuple[Any, list[str]]:
    actions: list[str] = []
    if isinstance(value, (int, float)):
        amount = Decimal(str(value))
    elif isinstance(value, str):
        text = value
        negative = False
        if m := _ACCOUNTING_NEGATIVE.match(text):
            text, negative = m.group(1).strip(), True
        m = _PRICE.match(text)
        if not m:
            # Words like "approx" or "about" mean an estimate, not a price. Refuse rather than strip them.
            raise Unparseable("unreadable_price", f"could not read {value!r} as a price")
        negative = negative or bool(m["neg1"] or m["neg2"])
        frac = m["frac"] or ""
        if not m["suffix"] and len(frac) > 2:
            raise Unparseable("unreadable_price", f"{value!r} has more than 2 decimal places")
        amount = Decimal(m["whole"].replace(",", "") + (f".{frac}" if frac else ""))
        if m["suffix"]:
            amount *= 1_000 if m["suffix"].lower() == "k" else 1_000_000
            actions.append("expanded_k_m_suffix")
        if negative:
            amount = -amount
        if re.search(r"[$,]|USD|\(", value):
            actions.append("removed_currency_formatting")
    else:
        raise Unparseable("wrong_type", f"expected a price, got {type(value).__name__}")

    try:
        cents = amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise Unparseable("unreadable_price", f"{value!r} is not a finite amount") from None
    if cents != amount:
        raise Unparseable("unreadable_price", f"{value!r} has fractions of a cent")
    return cents, actions


_MONTH_NAMES = ("january february march april may june july august "
                "september october november december").split()
_MONTHS = {"sept": 9}
for _i, _name in enumerate(_MONTH_NAMES, start=1):
    _MONTHS[_name] = _i  # "march"
    _MONTHS[_name[:3]] = _i  # "mar"
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?P<time>[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_YMD_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_MDY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_TWO_DIGIT_YEAR = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2}$")
_MONTH_NAME_FIRST = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")  # March 3, 2024
_DAY_FIRST_NAMED = re.compile(r"^(\d{1,2})[\s-]([A-Za-z]{3,9})\.?[\s-](\d{4})$")  # 3-Mar-2024


def _month(name: str, raw: str) -> int:
    month = _MONTHS.get(name.lower())
    if month is None:
        raise Unparseable("unreadable_date", f"{raw!r} has an unknown month name")
    return month


AMBIGUOUS_ORDER_ACTION = "assumed_month_first"


def _build_date(y: int, m: int, d: int, raw: str) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise Unparseable("impossible_date", f"{raw!r} is not a real calendar date") from None


def _date(value: Any, _: dict) -> tuple[Any, list[str]]:
    if not isinstance(value, str):
        # e.g. a Unix timestamp: seconds or milliseconds? Which timezone? Refuse to guess.
        raise Unparseable("unreadable_date", f"expected a date as text, got {type(value).__name__} {value!r}")
    actions = []
    if m := _ISO.match(value):
        result = _build_date(int(m[1]), int(m[2]), int(m[3]), value)
        if m["time"]:
            # Keep the calendar date as written. Converting the timezone first could move it a day.
            actions.append("dropped_time_of_day")
    elif m := _YMD_SLASH.match(value):
        result = _build_date(int(m[1]), int(m[2]), int(m[3]), value)
    elif m := _MDY.match(value):
        month, day = int(m[1]), int(m[2])
        if month > 12:
            # Proof that this source writes some dates day-first. The pipeline uses
            # this to re-check the file's ambiguous dates (see pipeline._recheck_date_order).
            raise Unparseable(
                "day_first_date",
                f"{value!r} is not a valid MM/DD/YYYY date; it may be DD/MM/YYYY, and we won't guess",
            )
        result = _build_date(int(m[3]), month, day, value)
        if day <= 12:
            # 05/02/2024 could be May 2 or Feb 5. We assume US order, but flag it
            # so the pipeline can withdraw that assumption if the file proves otherwise.
            actions.append(AMBIGUOUS_ORDER_ACTION)
    elif _TWO_DIGIT_YEAR.match(value):
        raise Unparseable("ambiguous_date", f"{value!r} has a two-digit year (19xx or 20xx?)")
    elif m := _MONTH_NAME_FIRST.match(value):
        result = _build_date(int(m[3]), _month(m[1], value), int(m[2]), value)
    elif m := _DAY_FIRST_NAMED.match(value):
        result = _build_date(int(m[3]), _month(m[2], value), int(m[1]), value)
    else:
        raise Unparseable("unreadable_date", f"could not read {value!r} as a date")
    if result.isoformat() != value and "dropped_time_of_day" not in actions:
        actions.append("reformatted_date")
    return result, actions


def _int_field(allow_sqft_unit: bool) -> Normalizer:
    unit = r"(?:\s*(?:sq\.?\s*ft\.?|sqft|sf|square\s+feet))?" if allow_sqft_unit else ""
    pattern = re.compile(rf"^(-?(?:\d{{1,3}}(?:,\d{{3}})+|\d+))(?:\.0+)?{unit}$", re.IGNORECASE)

    def normalize(value: Any, _: dict) -> tuple[Any, list[str]]:
        if isinstance(value, int):
            return value, []
        if isinstance(value, float):
            if value.is_integer():
                return int(value), []
            raise Unparseable("not_whole_number", f"{value!r} is not a whole number")
        if not isinstance(value, str):
            raise Unparseable("wrong_type", f"expected a number, got {type(value).__name__}")
        m = pattern.match(value)
        if not m:
            raise Unparseable("unreadable_number", f"could not read {value!r} as a whole number")
        digits = m.group(1).replace(",", "")
        return int(digits), (["removed_number_formatting"] if digits != value else [])

    return normalize


_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")


def _decimal(value: Any, _: dict) -> tuple[Any, list[str]]:
    if isinstance(value, (int, float)):
        return Decimal(str(value)), []
    if isinstance(value, str) and _DECIMAL.match(value):
        return Decimal(value), []
    raise Unparseable("unreadable_number", f"could not read {value!r} as a number")


_PROPERTY_TYPES = {
    "single family": "single_family", "single family residence": "single_family",
    "single family home": "single_family", "sfr": "single_family", "sfh": "single_family",
    "condo": "condo", "condominium": "condo",
    "townhouse": "townhouse", "townhome": "townhouse", "town house": "townhouse",
    "multi family": "multi_family", "multifamily": "multi_family", "duplex": "multi_family",
    "triplex": "multi_family", "fourplex": "multi_family",
    "land": "land", "lot": "land", "vacant land": "land",
}


def _property_type(value: Any, _: dict) -> tuple[Any, list[str]]:
    if not isinstance(value, str):
        raise Unparseable("wrong_type", f"expected text, got {type(value).__name__}")
    key = _NON_ALNUM.sub(" ", value.lower()).strip()
    mapped = _PROPERTY_TYPES.get(key)
    if mapped is None:
        raise Unparseable("unknown_property_type", f"{value!r} is not a recognized property type")
    return mapped, ([] if mapped == value else ["mapped_property_type"])


# Order matters: state must be normalized before zip (zip repair depends on it).
NORMALIZERS: dict[str, Normalizer] = {
    "property_id": _upper_code,
    "address": _text,
    "city": _text,
    "state": _state,
    "zip": _zip,
    "purchase_price": _price,
    "purchase_date": _date,
    "property_type": _property_type,
    "bedrooms": _int_field(allow_sqft_unit=False),
    "bathrooms": _decimal,
    "sqft": _int_field(allow_sqft_unit=True),
}
assert set(NORMALIZERS) == set(ALL_FIELDS), "every schema field needs a normalizer"


# ---------------------------------------------------------------- the row

def normalize_row(raw: dict[str, Any]) -> Normalized:
    result = Normalized()

    for column, value in raw.items():
        name = canonical_column(column)
        if name not in NORMALIZERS:
            result.fixes.append(Fix(column, "dropped_unknown_column", value, None))
            continue
        if name in result.raw_by_field and result.raw_by_field[name] != value:
            result.problems.append(Problem(
                name, "conflicting_columns",
                f"two columns both mean {name!r} but disagree: {result.raw_by_field[name]!r} vs {value!r}", value,
            ))
            continue
        result.raw_by_field[name] = value

    already_bad = {p.field for p in result.problems}
    for name, normalizer in NORMALIZERS.items():
        if name not in result.raw_by_field or name in already_bad:
            continue  # missing fields are the schema's call (some are optional)
        raw_value = result.raw_by_field[name]
        try:
            value, actions = _prepare(raw_value)
            if value is not None:
                value, more = normalizer(value, result.values)
                actions += more
        except Unparseable as e:
            result.problems.append(Problem(name, e.code, e.message, raw_value))
            continue
        result.values[name] = value
        result.fixes.extend(Fix(name, a, raw_value, value) for a in actions)

    return result
