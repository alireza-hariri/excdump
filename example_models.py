"""Pydantic models used by the example.

They live in an importable module rather than in the script that fails, which
is what production code looks like anyway and is what lets a dump keep the
model *objects*: a class reachable by import is stored as a reference, so the
inspector rebuilds real instances and ``cart.subtotal()`` still runs offline.
Classes defined in ``__main__`` cannot be stored that way and degrade to a repr
string in the dump.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Customer(BaseModel):
    name: str
    request_id: str
    tier: Literal["standard", "gold"] = "standard"


class LineItem(BaseModel):
    sku: str
    price: float = Field(gt=0)
    quantity: int = Field(default=1, ge=1)

    @property
    def total(self) -> float:
        return self.price * self.quantity


class Cart(BaseModel):
    customer: Customer
    items: List[LineItem]

    def subtotal(self) -> float:
        return sum(item.total for item in self.items)


class ShippingQuote(BaseModel):
    zone: str
    divisor: int
    discounted_total: float
    cost: Optional[float] = None

    @field_validator("zone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        if value not in {"domestic", "international"}:
            raise ValueError(f"unknown shipping zone {value!r}")
        return value
