"""Unit tests for pipeline helpers that do not need a database."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from example_1.config import Settings
from example_1.models import RawLine
from example_1.pipeline import (
    ORDER_COLUMNS,
    RowRejectedError,
    SchemaDriftError,
    build_parser,
    file_sha256,
    main,
    read_csv_lines,
    validate_order_line,
    validate_product_line,
)

HEADER = ",".join(ORDER_COLUMNS)


def _line(text: str, line_no: int = 2) -> RawLine:
    """Build a RawLine from comma-separated text (no quoting)."""
    return RawLine(line_no=line_no, raw_line=text, fields=tuple(text.split(",")))


def test_read_csv_lines_keeps_original_text_and_skips_blank_lines(tmp_path: Path) -> None:
    """Quoted fields are parsed but the raw line is kept verbatim; blank lines are skipped."""
    path = tmp_path / "orders.csv"
    row = 'O1,2024-01-01,C1,P1,1,"$1,099.00",West'
    path.write_text(f"{HEADER}\n{row}\n\n", encoding="utf-8")
    [line] = read_csv_lines(path, ORDER_COLUMNS)
    assert line.raw_line == row
    assert line.fields[5] == "$1,099.00"
    assert line.line_no == 2


def test_read_csv_lines_rejects_header_drift(tmp_path: Path) -> None:
    """A renamed or reordered column fails loudly instead of loading shifted data."""
    path = tmp_path / "orders.csv"
    path.write_text(HEADER.replace("unit_price", "price") + "\n", encoding="utf-8")
    with pytest.raises(SchemaDriftError, match="expected header"):
        read_csv_lines(path, ORDER_COLUMNS)


def test_read_csv_lines_rejects_empty_file(tmp_path: Path) -> None:
    """An empty drop is treated as a schema problem, not as a day with no sales."""
    path = tmp_path / "orders.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SchemaDriftError, match="empty"):
        read_csv_lines(path, ORDER_COLUMNS)


def test_validate_order_line_rejects_truncated_row() -> None:
    """A row with missing trailing fields is quarantined as truncated."""
    with pytest.raises(RowRejectedError) as info:
        validate_order_line(_line("O2009,2024-01-02,C019,P004,2,"))
    assert info.value.reason == "truncated_row"


def test_validate_order_line_ignores_extra_trailing_fields() -> None:
    """Extra trailing fields are tolerated; the first seven columns are used."""
    order = validate_order_line(_line("O1011,2024-01-01,C010,P001,1,24.99,East,EXTRA_FLAG"))
    assert order.region == "East"


def test_validate_order_line_reports_field_errors() -> None:
    """Field validation failures surface as RowRejectedError with the field reason."""
    with pytest.raises(RowRejectedError) as info:
        validate_order_line(_line("O1008,2024-01-01,C007,P008,0,9.99,North"))
    assert info.value.reason == "quantity:zero_quantity"


def test_validate_product_line_checks_field_count_and_values() -> None:
    """Product rows need exactly four fields and valid values."""
    with pytest.raises(RowRejectedError) as info:
        validate_product_line(_line("P1,Thing,Kitchen"))
    assert info.value.reason == "wrong_field_count"
    with pytest.raises(RowRejectedError) as info:
        validate_product_line(_line("P1,Thing,Kitchen,abc"))
    assert info.value.reason == "list_price:bad_money"
    assert validate_product_line(_line("p1,Thing,kitchen,1.5")).category == "Kitchen"


def test_file_sha256_changes_with_content(tmp_path: Path) -> None:
    """The ledger hash detects any change in file content."""
    path = tmp_path / "f.csv"
    path.write_text("a", encoding="utf-8")
    first = file_sha256(path)
    path.write_text("b", encoding="utf-8")
    assert file_sha256(path) != first


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings come from PIPELINE_* environment variables."""
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://u@h/db")
    monkeypatch.setenv("PIPELINE_DATA_DIR", "landing")
    settings = Settings()
    assert settings.database_url == "postgresql://u@h/db"
    assert settings.data_dir == Path("landing")


def test_build_parser_leaves_unset_options_empty() -> None:
    """Unset CLI options are None so environment settings apply."""
    args = build_parser().parse_args([])
    assert args.database_url is None
    assert args.data_dir is None


def test_main_returns_1_when_products_file_is_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing product reference fails cleanly before touching the database."""
    caplog.set_level(logging.ERROR)
    assert main(["--data-dir", str(tmp_path), "--out-dir", str(tmp_path / "out")]) == 1
    assert "products.csv not found" in caplog.text


def test_main_returns_2_when_database_is_unreachable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A database failure exits with code 2 and is logged, not raised."""
    caplog.set_level(logging.ERROR)
    (tmp_path / "products.csv").write_text("product_id\n", encoding="utf-8")
    code = main(
        [
            "--database-url",
            "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1",
            "--data-dir",
            str(tmp_path),
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2
    assert "database error" in caplog.text
