"""Tests for the raw value parsers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from example_1.parsing import ParseError, parse_money, parse_order_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-01-02", date(2024, 1, 2)),
        ("01/02/2024", date(2024, 1, 2)),
        ("1704153600", date(2024, 1, 2)),
        ("  2024-01-01 ", date(2024, 1, 1)),
    ],
)
def test_parse_order_date_supported_formats(raw: str, expected: date) -> None:
    """ISO, US month-first and epoch (UTC) dates all parse to the same calendar date."""
    assert parse_order_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "garbage", "2024-13-01", "13/01/2024", "02.01.2024"])
def test_parse_order_date_rejects_unknown_formats(raw: str) -> None:
    """Unparseable or impossible dates raise ParseError with a stable code."""
    with pytest.raises(ParseError) as info:
        parse_order_date(raw)
    assert info.value.code == "bad_order_date"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,099.00", Decimal("1099.00")),
        ("3.50", Decimal("3.50")),
        ("3.5", Decimal("3.50")),
        (" 8.99 ", Decimal("8.99")),
        ("-12", Decimal("-12.00")),
    ],
)
def test_parse_money_strips_symbols_and_keeps_exact_cents(raw: str, expected: Decimal) -> None:
    """Currency symbols and separators are removed and the value stays an exact Decimal."""
    result = parse_money(raw)
    assert result == expected
    assert result.as_tuple().exponent == -2


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "bad_money"),
        ("abc", "bad_money"),
        ("NaN", "bad_money"),
        ("Infinity", "bad_money"),
        ("1.999", "sub_cent_precision"),
    ],
)
def test_parse_money_rejects_invalid_amounts(raw: str, code: str) -> None:
    """Non-numeric, non-finite and sub-cent amounts are rejected with distinct codes."""
    with pytest.raises(ParseError) as info:
        parse_money(raw)
    assert info.value.code == code
