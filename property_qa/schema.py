"""Stage 3 of 3 (the rules): the blueprint every clean record must match.

strict=True is deliberate: Pydantic will NOT quietly convert "3" into 3 or
True into 1. Every conversion happens in normalize.py, where it is logged.
So if data reaches this model in the wrong shape, it is rejected, not
silently "helped".

extra="forbid" means an unexpected field is an error, not a passenger.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

PropertyType = Literal["single_family", "condo", "townhouse", "multi_family", "land"]


class PropertyRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    property_id: str = Field(pattern=r"^[A-Z]{2}-\d{4,8}$")
    address: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    state: str
    zip: str = Field(pattern=r"^\d{5}$")
    # Decimal, never float: 0.1 + 0.2 != 0.3 in floating point, and money must add up exactly.
    purchase_price: Decimal = Field(gt=0, le=Decimal("1000000000"), decimal_places=2)
    purchase_date: date
    property_type: PropertyType
    bedrooms: int | None = Field(default=None, ge=0, le=50)
    # Quarter steps because real estate has quarter (.25) and three-quarter (.75) baths.
    bathrooms: Decimal | None = Field(default=None, ge=0, le=50, multiple_of=Decimal("0.25"))
    sqft: int | None = Field(default=None, gt=0, le=1_000_000)

    @field_validator("state")
    @classmethod
    def _known_state(cls, v: str) -> str:
        if v not in US_STATES:
            raise ValueError(f"{v!r} is not a US state code")
        return v

    @field_validator("purchase_date")
    @classmethod
    def _plausible_date(cls, v: date) -> date:
        if v < date(1900, 1, 1):
            raise ValueError("is before 1900")
        if v > date.today():
            raise ValueError("is in the future")
        return v

    @model_validator(mode="after")
    def _land_has_no_rooms(self) -> PropertyRecord:
        # A rule across two fields: each value is fine alone, the combination is not.
        if self.property_type == "land" and (self.bedrooms or self.bathrooms):
            raise ValueError("land parcels cannot have bedrooms or bathrooms")
        return self


REQUIRED_FIELDS = tuple(name for name, f in PropertyRecord.model_fields.items() if f.is_required())
ALL_FIELDS = tuple(PropertyRecord.model_fields)
