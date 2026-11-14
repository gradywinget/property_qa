"""The schema's business rules, tested against already-normalized values."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from property_qa.schema import PropertyRecord

GOOD = dict(
    property_id="CA-100201", address="123 Main St", city="San Francisco", state="CA", zip="94110",
    purchase_price=Decimal("1250000.00"), purchase_date=date(2024, 3, 15), property_type="single_family",
    bedrooms=3, bathrooms=Decimal("2"), sqft=1850,
)


def errors(**overrides):
    with pytest.raises(ValidationError) as e:
        PropertyRecord.model_validate({**GOOD, **overrides})
    return [(err["loc"][0] if err["loc"] else "__record__", err["type"]) for err in e.value.errors()]


def test_good_record():
    assert PropertyRecord.model_validate(GOOD).purchase_price == Decimal("1250000.00")


def test_optional_fields_may_be_missing():
    PropertyRecord.model_validate({k: v for k, v in GOOD.items() if k not in ("bedrooms", "bathrooms", "sqft")})


@pytest.mark.parametrize("overrides, expected", [
    ({"purchase_price": Decimal("-450000.00")}, ("purchase_price", "greater_than")),
    ({"purchase_price": Decimal("0.00")}, ("purchase_price", "greater_than")),
    ({"purchase_price": Decimal("2000000000.00")}, ("purchase_price", "less_than_equal")),
    ({"purchase_price": 1250000.0}, ("purchase_price", "is_instance_of")),  # float refused in strict mode
    ({"property_id": "ca-100201"}, ("property_id", "string_pattern_mismatch")),
    ({"zip": "9411"}, ("zip", "string_pattern_mismatch")),
    ({"state": "ZZ"}, ("state", "value_error")),
    ({"bathrooms": Decimal("2.3")}, ("bathrooms", "multiple_of")),
    ({"bedrooms": "3"}, ("bedrooms", "int_type")),  # strict: no silent str -> int
    ({"bedrooms": True}, ("bedrooms", "int_type")),
    ({"sqft": 0}, ("sqft", "greater_than")),
    ({"property_type": "mobile_home"}, ("property_type", "literal_error")),
    ({"purchase_date": date(1850, 1, 1)}, ("purchase_date", "value_error")),
    ({"purchase_date": date.today() + timedelta(days=1)}, ("purchase_date", "value_error")),
    ({"extra_field": "x"}, ("extra_field", "extra_forbidden")),
])
def test_rules(overrides, expected):
    assert expected in errors(**overrides)


def test_quarter_baths_allowed():
    assert PropertyRecord.model_validate({**GOOD, "bathrooms": Decimal("1.75")}).bathrooms == Decimal("1.75")


def test_land_with_rooms_rejected():
    assert ("__record__", "value_error") in errors(property_type="land", bedrooms=3, bathrooms=None)
    PropertyRecord.model_validate({**GOOD, "property_type": "land", "bedrooms": None, "bathrooms": None})
