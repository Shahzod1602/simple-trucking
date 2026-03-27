import re
from typing import Literal, Optional
from pydantic import BaseModel, field_validator


class Address(BaseModel):
    address_line_1: Optional[str] = None
    address_line_2: Optional[str] = None
    city: str
    state: str
    zip: str
    country: str = "USA"


class Stop(BaseModel):
    address: Address
    date: str
    type: Literal["pickup", "delivery"]
    reference: Optional[str] = None

    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        if not re.match(r"^\d{2}/\d{2}/\d{4} \d{3,4}$", v):
            raise ValueError(f"Date must be MM/DD/YYYY HHMM format, got: '{v}'")
        return v


class RateCon(BaseModel):
    broker: str
    carrier: str
    total_rate_usd: str
    load_number: str
    customer_ref: Optional[str] = None
    commodity: Optional[str] = None
    weight: Optional[str] = None
    miles: Optional[str] = None
    stops: list[Stop]

    @field_validator("total_rate_usd")
    @classmethod
    def validate_rate(cls, v: str) -> str:
        if not v.startswith("$"):
            raise ValueError(f"total_rate_usd must start with '$', got: '{v}'")
        return v

    @field_validator("stops")
    @classmethod
    def validate_stops(cls, v: list[Stop]) -> list[Stop]:
        if len(v) < 2:
            raise ValueError("Ratecon must have at least 2 stops (pickup + delivery)")
        types = [s.type for s in v]
        if "pickup" not in types:
            raise ValueError("No pickup stop found")
        if "delivery" not in types:
            raise ValueError("No delivery stop found")
        return v
