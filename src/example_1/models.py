"""Pydantic models for raw lines, validated orders and products, and pipeline results."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from example_1.parsing import ParseError, parse_money, parse_order_date

UNKNOWN_CATEGORY = "Unknown"
"""Category used in gold for orders whose product is missing from the product reference."""


def _required_text(value: object) -> str:
    """Return stripped text, rejecting missing or blank values.

    Args:
        value: Raw value from the CSV row.

    Returns:
        The stripped string.

    Raises:
        PydanticCustomError: ``missing_value`` if blank, ``not_text`` if not a string.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise PydanticCustomError("missing_value", "a value is required")
    if not isinstance(value, str):
        raise PydanticCustomError("not_text", "expected text")
    return value.strip()


def _parse_money_field(value: object) -> object:
    """Parse a money field, passing non-text values (e.g. Decimal) through to Pydantic.

    Args:
        value: Raw value from the CSV row or an already-typed value.

    Returns:
        A Decimal for text input, otherwise the value unchanged.

    Raises:
        PydanticCustomError: If the text is blank or not a valid amount.
    """
    if isinstance(value, Decimal | int):
        return value
    text = _required_text(value)
    try:
        return parse_money(text)
    except ParseError as exc:
        raise PydanticCustomError(exc.code, "{detail}", {"detail": str(exc)}) from None


class RawLine(BaseModel):
    """One physical line of a source CSV file, kept exactly as delivered.

    Attributes:
        line_no: 1-based line number in the file (the header is line 1).
        raw_line: The original text of the line, quoting included.
        fields: The line split into CSV fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_no: int = Field(..., ge=2, description="Line number in the source file")
    raw_line: str = Field(..., description="Original line text")
    fields: tuple[str, ...] = Field(..., description="Parsed CSV fields")

    @property
    def n_fields(self) -> int:
        """Number of CSV fields on the line."""
        return len(self.fields)


class OrderRecord(BaseModel):
    """A validated, normalised order row ready for the silver layer.

    Attributes:
        order_id: Source-system order identifier (the business key).
        order_date: Date the order was placed.
        customer_id: Customer identifier; optional because the source sometimes omits it.
        product_id: Upper-cased product identifier joining to the product reference.
        quantity: Units; negative values are returns, zero is rejected.
        unit_price: Positive price per unit at time of sale, exact to the cent.
        region: Sales region in title case (``west`` and ``West`` are the same region).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str = Field(..., min_length=1, description="Source order identifier")
    order_date: date = Field(..., description="Date the order was placed")
    customer_id: str | None = Field(default=None, description="Customer identifier, if known")
    product_id: str = Field(..., min_length=1, description="Product identifier")
    quantity: int = Field(..., description="Units sold; negative for returns")
    unit_price: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    region: str = Field(..., min_length=1, description="Sales region")

    @field_validator("order_id", mode="before")
    @classmethod
    def _clean_order_id(cls, value: object) -> str:
        """Require and strip the order ID."""
        return _required_text(value)

    @field_validator("product_id", mode="before")
    @classmethod
    def _clean_product_id(cls, value: object) -> str:
        """Require, strip and upper-case the product ID."""
        return _required_text(value).upper()

    @field_validator("region", mode="before")
    @classmethod
    def _clean_region(cls, value: object) -> str:
        """Require the region and normalise it to title case."""
        return _required_text(value).title()

    @field_validator("customer_id", mode="before")
    @classmethod
    def _clean_customer_id(cls, value: object) -> object:
        """Strip the customer ID and treat blanks as unknown."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("order_date", mode="before")
    @classmethod
    def _parse_order_date(cls, value: object) -> object:
        """Parse any of the supported date formats."""
        if isinstance(value, date):
            return value
        text = _required_text(value)
        try:
            return parse_order_date(text)
        except ParseError as exc:
            raise PydanticCustomError(exc.code, "{detail}", {"detail": str(exc)}) from None

    @field_validator("unit_price", mode="before")
    @classmethod
    def _parse_unit_price(cls, value: object) -> object:
        """Parse prices such as ``$1,099.00``."""
        return _parse_money_field(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def _parse_quantity(cls, value: object) -> object:
        """Parse the quantity as a whole number."""
        if isinstance(value, int):
            return value
        text = _required_text(value)
        try:
            return int(text)
        except ValueError:
            raise PydanticCustomError("bad_quantity", "quantity must be a whole number") from None

    @field_validator("quantity")
    @classmethod
    def _reject_zero_quantity(cls, value: int) -> int:
        """Reject zero quantities: they are neither a sale nor a return."""
        if value == 0:
            raise PydanticCustomError("zero_quantity", "quantity of zero is not a sale")
        return value


class Product(BaseModel):
    """A validated row from the product reference file.

    Attributes:
        product_id: Upper-cased product identifier.
        product_name: Display name.
        category: Reporting category in title case (``kitchen`` and ``Kitchen`` match).
        list_price: Catalogue list price (informational; revenue uses the sale price).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: str = Field(..., min_length=1)
    product_name: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    list_price: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)

    @field_validator("product_id", mode="before")
    @classmethod
    def _clean_product_id(cls, value: object) -> str:
        """Require, strip and upper-case the product ID."""
        return _required_text(value).upper()

    @field_validator("product_name", mode="before")
    @classmethod
    def _clean_product_name(cls, value: object) -> str:
        """Require and strip the product name."""
        return _required_text(value)

    @field_validator("category", mode="before")
    @classmethod
    def _clean_category(cls, value: object) -> str:
        """Require the category and normalise it to title case."""
        return _required_text(value).title()

    @field_validator("list_price", mode="before")
    @classmethod
    def _parse_list_price(cls, value: object) -> object:
        """Parse the list price."""
        return _parse_money_field(value)


class DailySales(BaseModel):
    """One row of the gold ``daily_sales`` table.

    Attributes:
        order_date: Day of the orders.
        category: Product category, or ``Unknown`` for unmatched products.
        region: Sales region.
        net_revenue: Sum of quantity x unit price, returns included.
        order_count: Distinct orders with positive quantity.
        units_sold: Net units (returns subtract).
        aov: Net revenue / order count, rounded half-up to cents; None when there are no orders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_date: date
    category: str
    region: str
    net_revenue: Decimal
    order_count: int = Field(..., ge=0)
    units_sold: int
    aov: Decimal | None


class FileLoadResult(BaseModel):
    """Outcome of loading one daily order file.

    Attributes:
        source_file: File name, e.g. ``orders_2024-01-02.csv``.
        rows_read: Data lines read from the file.
        rows_clean: Lines that passed validation and were upserted into silver.
        rows_quarantined: Lines rejected into quarantine.
        affected_dates: Order dates whose gold rows must be rebuilt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_file: str
    rows_read: int = Field(..., ge=0)
    rows_clean: int = Field(..., ge=0)
    rows_quarantined: int = Field(..., ge=0)
    affected_dates: frozenset[date] = frozenset()


class RunSummary(BaseModel):
    """Outcome of one pipeline run.

    Attributes:
        files_loaded: Per-file results for newly loaded order files (empty on a no-op run).
        products_reloaded: Whether the product reference changed and was reloaded.
        dates_refreshed: Order dates whose gold rows were rebuilt.
        gold_rows_written: Gold rows inserted during the rebuild.
        export_path: Where the gold table was exported as CSV.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    files_loaded: tuple[FileLoadResult, ...]
    products_reloaded: bool
    dates_refreshed: tuple[date, ...]
    gold_rows_written: int = Field(..., ge=0)
    export_path: Path


def rejection_reason(error: ValidationError) -> str:
    """Turn a Pydantic validation error into a compact, stable quarantine reason.

    Args:
        error: The validation error raised for a row.

    Returns:
        ``field:error_type`` pairs joined by ``; `` (e.g. ``quantity:zero_quantity``).
    """
    parts = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "row"
        parts.append(f"{location}:{detail['type']}")
    return "; ".join(parts)
