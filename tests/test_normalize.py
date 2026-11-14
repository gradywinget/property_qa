"""Unit tests for the normalizer: what it may fix, and what it must refuse to guess."""

from datetime import date
from decimal import Decimal

import pytest

from property_qa.normalize import canonical_column, normalize_row


def norm(**fields):
    return normalize_row(fields)


def value_of(field, raw, **context):
    n = normalize_row({**context, field: raw})
    assert not n.problems, n.problems
    return n.values[field]


def problem_code(field, raw, **context):
    n = normalize_row({**context, field: raw})
    codes = [p.code for p in n.problems if p.field == field]
    assert codes, f"expected a problem for {raw!r}, got value {n.values.get(field)!r}"
    return codes[0]


@pytest.mark.parametrize("raw, expected", [
    ("Sale Price", "purchase_price"),
    ("salePrice", "purchase_price"),
    ("  sale-price ", "purchase_price"),
    ("postalCode", "zip"),
    ("Sq Ft", "sqft"),
    ("Parcel ID", "property_id"),
    ("Notes", "notes"),  # unknown names pass through and get dropped later
])
def test_column_names(raw, expected):
    assert canonical_column(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("$1,250,000", Decimal("1250000.00")),
    ("850000", Decimal("850000.00")),
    ("640,000.00", Decimal("640000.00")),
    ("USD 525,000", Decimal("525000.00")),
    ("525,000 USD", Decimal("525000.00")),
    ("$2.1M", Decimal("2100000.00")),
    ("$615K", Decimal("615000.00")),
    (649999.99, Decimal("649999.99")),
    (715000, Decimal("715000.00")),
    ("-450000", Decimal("-450000.00")),  # readable; the schema rejects it as negative
    ("(450,000)", Decimal("-450000.00")),  # accounting notation
])
def test_price_readable(raw, expected):
    assert value_of("purchase_price", raw) == expected


@pytest.mark.parametrize("raw, code", [
    ("$1,2O0,000", "unreadable_price"),  # letter O, not zero
    ("approx $500k", "unreadable_price"),  # an estimate is not a price
    ("1,25,000", "unreadable_price"),  # misplaced comma
    ("1.200.000,00", "unreadable_price"),  # European notation: refuse, don't guess
    ("450000.125", "unreadable_price"),
    ("#VALUE!", "upstream_error_code"),
])
def test_price_refused(raw, code):
    assert problem_code("purchase_price", raw) == code


@pytest.mark.parametrize("raw, expected", [
    ("2024-03-15", date(2024, 3, 15)),
    ("2024-02-14T00:00:00Z", date(2024, 2, 14)),
    ("2024-02-14T23:30:00-08:00", date(2024, 2, 14)),  # the date as written, not shifted to UTC
    ("2024/03/15", date(2024, 3, 15)),
    ("03/15/2024", date(2024, 3, 15)),
    ("2/29/2024", date(2024, 2, 29)),
    ("March 3, 2024", date(2024, 3, 3)),
    ("Mar 3 2024", date(2024, 3, 3)),
    ("Sept 9, 2024", date(2024, 9, 9)),
    ("12-Apr-2024", date(2024, 4, 12)),
])
def test_date_readable(raw, expected):
    assert value_of("purchase_date", raw) == expected


@pytest.mark.parametrize("raw, code", [
    ("CRITICAL_TIMESTAMP_PARSE_ERROR", "upstream_error_code"),
    ("13/05/2024", "day_first_date"),
    ("3/4/24", "ambiguous_date"),
    ("2/30/2024", "impossible_date"),
    ("2/29/2023", "impossible_date"),  # not a leap year
    ("Smarch 3, 2024", "unreadable_date"),
    ("yesterday", "unreadable_date"),
])
def test_date_refused(raw, code):
    assert problem_code("purchase_date", raw) == code


def test_error_code_is_never_replaced_with_a_timestamp():
    n = norm(purchase_date="CRITICAL_TIMESTAMP_PARSE_ERROR")
    assert "purchase_date" not in n.values
    assert n.problems[0].raw_value == "CRITICAL_TIMESTAMP_PARSE_ERROR"


def test_ambiguous_date_is_flagged_for_the_file_level_check():
    n = norm(purchase_date="05/02/2024")
    assert n.values["purchase_date"] == date(2024, 5, 2)
    assert any(f.action == "assumed_month_first" for f in n.fixes)
    assert not any(f.action == "assumed_month_first" for f in norm(purchase_date="05/22/2024").fixes)


def test_unix_timestamp_refused():
    assert problem_code("purchase_date", 1710460800) == "unreadable_date"


@pytest.mark.parametrize("raw, state, expected", [
    ("94110", "CA", "94110"),
    ("10011-4402", "NY", "10011"),
    ("2134", "MA", "02134"),  # Excel dropped the leading zero; MA ZIPs start with 0
    (2134, "MA", "02134"),
    ("94110.0", "CA", "94110"),
    (97209, "OR", "97209"),
])
def test_zip_readable(raw, state, expected):
    assert value_of("zip", raw, state=state) == expected


def test_four_digit_zip_outside_new_england_is_not_guessed():
    # "9040" in CA: a digit is missing, and padding it would invent "09040" (a NJ ZIP).
    assert problem_code("zip", "9040", state="CA") == "ambiguous_zip"


@pytest.mark.parametrize("raw, expected", [("California", "CA"), ("tx", "TX"), ("New York", "NY"), ("ZZ", "ZZ")])
def test_state(raw, expected):
    assert value_of("state", raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("SFR", "single_family"), ("Single Family Home", "single_family"), ("Multi-Family", "multi_family"),
    ("Townhome", "townhouse"), ("Duplex", "multi_family"), ("condo", "condo"),
])
def test_property_type(raw, expected):
    assert value_of("property_type", raw) == expected


def test_unknown_property_type():
    assert problem_code("property_type", "Mobile Home") == "unknown_property_type"


@pytest.mark.parametrize("field, raw, expected", [
    ("sqft", "1,850", 1850), ("sqft", "1,100 sqft", 1100), ("sqft", "1450 sq. ft.", 1450),
    ("bedrooms", "3", 3), ("bedrooms", 3.0, 3), ("bathrooms", "2.5", Decimal("2.5")),
])
def test_numbers(field, raw, expected):
    assert value_of(field, raw) == expected


def test_units_only_allowed_on_sqft():
    assert problem_code("bedrooms", "3 sqft") == "unreadable_number"
    assert problem_code("bedrooms", "three") == "unreadable_number"


def test_whitespace_and_invisible_characters():
    n = norm(address="  410 Colfax   Ave ", city="Denver​")
    assert n.values == {"address": "410 Colfax Ave", "city": "Denver"}
    assert {f.action for f in n.fixes} == {"cleaned_whitespace"}


def test_city_case_is_not_changed():
    assert value_of("city", "McAllen") == "McAllen"


@pytest.mark.parametrize("token", ["", "N/A", "n/a", "null", "NaN", "-", "??", "TBD"])
def test_null_tokens_become_missing(token):
    assert norm(bedrooms=token).values["bedrooms"] is None


def test_unknown_columns_are_dropped_and_logged():
    n = norm(notes="great view")
    assert n.values == {}
    assert n.fixes[0].action == "dropped_unknown_column"


def test_conflicting_columns_for_same_field():
    n = normalize_row({"price": "500000", "sale_price": "550000"})
    assert n.problems[0].code == "conflicting_columns"
    assert not normalize_row({"price": "500000", "sale_price": "500000"}).problems


def test_booleans_are_not_numbers():
    assert problem_code("bedrooms", True) == "wrong_type"
