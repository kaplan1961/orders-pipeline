"""Parsers for the messy text values found in the raw order and product files."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

_EPOCH_RE: Final = re.compile(r"^\d{9,10}$")
_DATE_FORMATS: Final = ("%Y-%m-%d", "%m/%d/%Y")
_CENT: Final = Decimal("0.01")


class ParseError(ValueError):
    """A raw text value could not be parsed.

    Attributes:
        code: Machine-readable reason, stored with quarantined rows (e.g. ``bad_order_date``).
    """

    def __init__(self, code: str, message: str) -> None:
        """Create the error.

        Args:
            code: Machine-readable reason code.
            message: Human-readable explanation including the offending value.
        """
        super().__init__(message)
        self.code = code


def parse_order_date(raw: str) -> date:
    """Parse an order date delivered in any of the formats seen in the source files.

    Supported formats are ISO (``2024-01-02``), US month-first (``01/02/2024``) and Unix
    epoch seconds (``1704153600``), which is interpreted as UTC.

    Args:
        raw: The raw text value.

    Returns:
        The calendar date of the order.

    Raises:
        ParseError: If the value matches none of the supported formats.

    Example:
        >>> parse_order_date("01/02/2024")
        datetime.date(2024, 1, 2)
    """
    value = raw.strip()
    if _EPOCH_RE.fullmatch(value):
        return datetime.fromtimestamp(int(value), tz=UTC).date()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ParseError("bad_order_date", f"unrecognised date: {raw!r}")


def parse_money(raw: str) -> Decimal:
    """Parse a monetary amount such as ``$1,099.00`` or ``3.5`` into an exact Decimal.

    Currency symbols and thousands separators are stripped. Amounts are kept exact (never
    floats) and must have at most two decimal places.

    Args:
        raw: The raw text value.

    Returns:
        The amount quantised to cents.

    Raises:
        ParseError: If the value is not a finite number or has sub-cent precision.

    Example:
        >>> parse_money("$1,099.00")
        Decimal('1099.00')
    """
    value = raw.strip().replace("$", "").replace(",", "")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ParseError("bad_money", f"not a monetary amount: {raw!r}") from None
    if not amount.is_finite():
        raise ParseError("bad_money", f"not a finite amount: {raw!r}")
    if amount != amount.quantize(_CENT):
        raise ParseError("sub_cent_precision", f"more than two decimal places: {raw!r}")
    return amount.quantize(_CENT)
