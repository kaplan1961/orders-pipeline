"""Tests for the Pydantic models and quarantine reason formatting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from example_1.models import OrderRecord, Product, RawLine, rejection_reason

VALID_ORDER = {
    "order_id": " O1 ",
    "order_date": "01/02/2024",
    "customer_id": "C1",
    "product_id": "p001",
    "quantity": "2",
    "unit_price": "$1,099.00",
    "region": "west",
}


def _reason(**overrides: object) -> str:
    """Validate VALID_ORDER with overrides and return the rejection reason."""
    with pytest.raises(ValidationError) as info:
        OrderRecord.model_validate({**VALID_ORDER, **overrides})
    return rejection_reason(info.value)


def test_order_record_normalises_raw_text() -> None:
    """IDs are stripped/upper-cased, region title-cased, date and price parsed."""
    order = OrderRecord.model_validate(VALID_ORDER)
    assert order.order_id == "O1"
    assert order.product_id == "P001"
    assert order.region == "West"
    assert order.order_date == date(2024, 1, 2)
    assert order.unit_price == Decimal("1099.00")
    assert order.quantity == 2


def test_order_record_accepts_typed_values() -> None:
    """Already-typed Python values pass straight through the validators."""
    order = OrderRecord(
        order_id="O1",
        order_date=date(2024, 1, 1),
        product_id="P1",
        quantity=-1,
        unit_price=Decimal("5.00"),
        region="East",
    )
    assert order.quantity == -1
    assert order.customer_id is None


def test_order_record_blank_customer_is_none() -> None:
    """A missing customer ID is allowed and stored as None."""
    assert OrderRecord.model_validate({**VALID_ORDER, "customer_id": "  "}).customer_id is None


def test_order_record_is_immutable() -> None:
    """Validated orders are frozen."""
    order = OrderRecord.model_validate(VALID_ORDER)
    with pytest.raises(ValidationError):
        order.quantity = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"quantity": "0"}, "quantity:zero_quantity"),
        ({"quantity": "two"}, "quantity:bad_quantity"),
        ({"quantity": ""}, "quantity:missing_value"),
        ({"unit_price": "0"}, "unit_price:greater_than"),
        ({"unit_price": "-1.00"}, "unit_price:greater_than"),
        ({"unit_price": "free"}, "unit_price:bad_money"),
        ({"unit_price": None}, "unit_price:missing_value"),
        ({"order_date": "yesterday"}, "order_date:bad_order_date"),
        ({"order_date": ""}, "order_date:missing_value"),
        ({"region": " "}, "region:missing_value"),
        ({"product_id": ""}, "product_id:missing_value"),
        ({"order_id": 5}, "order_id:not_text"),
    ],
)
def test_order_record_rejections_have_stable_reasons(
    overrides: dict[str, object], expected: str
) -> None:
    """Each validation rule produces a predictable ``field:code`` quarantine reason."""
    assert _reason(**overrides) == expected


def test_rejection_reason_joins_multiple_errors() -> None:
    """Several failing fields are all reported, in field order."""
    assert _reason(quantity="0", region="") == "quantity:zero_quantity; region:missing_value"


def test_product_normalises_category_and_price() -> None:
    """Categories are title-cased so ``electronics`` and ``Electronics`` group together."""
    product = Product.model_validate(
        {
            "product_id": "p002",
            "product_name": " USB-C Cable ",
            "category": "electronics",
            "list_price": "8.99",
        }
    )
    assert product == Product(
        product_id="P002",
        product_name="USB-C Cable",
        category="Electronics",
        list_price=Decimal("8.99"),
    )


def test_product_requires_category() -> None:
    """A product without a category cannot be reported on and is rejected."""
    with pytest.raises(ValidationError) as info:
        Product.model_validate(
            {"product_id": "P1", "product_name": "X", "category": "", "list_price": "1"}
        )
    assert rejection_reason(info.value) == "category:missing_value"


def test_raw_line_counts_fields() -> None:
    """RawLine exposes the number of parsed fields."""
    assert RawLine(line_no=2, raw_line="a,b", fields=("a", "b")).n_fields == 2
