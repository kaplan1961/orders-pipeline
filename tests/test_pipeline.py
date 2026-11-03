"""End-to-end tests of the pipeline against a real PostgreSQL database."""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from example_1.config import Settings
from example_1.models import DailySales
from example_1.pipeline import (
    ReconciliationError,
    SchemaDriftError,
    SourceFileChangedError,
    fetch_gold,
    main,
    reconcile_gold,
    run,
)

HEADER = "order_id,order_date,customer_id,product_id,quantity,unit_price,region\n"
D1, D2, D3 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)

type GoldKey = tuple[date, str, str]
type GoldValue = tuple[str, int, int, str | None]

EXPECTED_GOLD: dict[GoldKey, GoldValue] = {
    (D1, "Accessories", "South"): ("49.95", 1, 1, "49.95"),
    (D1, "Electronics", "East"): ("74.97", 2, 3, "37.49"),  # 37.485 rounds half-up
    (D1, "Electronics", "West"): ("2287.90", 3, 12, "762.63"),
    (D1, "Home & Office", "West"): ("29.99", 1, 1, "29.99"),
    (D1, "Kitchen", "East"): ("36.00", 1, 3, "36.00"),
    (D1, "Kitchen", "South"): ("30.00", 1, 4, "30.00"),
    (D1, "Stationery", "North"): ("21.00", 1, 6, "21.00"),  # O1003 restated 5 -> 6 by day 2
    (D1, "Unknown", "East"): ("30.00", 1, 2, "30.00"),  # P099 not in products
    (D2, "Accessories", "West"): ("49.95", 1, 1, "49.95"),  # epoch-dated O2003
    (D2, "Electronics", "East"): ("74.97", 1, 3, "74.97"),
    (D2, "Electronics", "North"): ("44.95", 1, 5, "44.95"),
    (D2, "Electronics", "West"): ("-1099.00", 0, -1, None),  # return only: no orders, no AOV
    (D2, "Kitchen", "South"): ("24.00", 1, 2, "24.00"),
    (D2, "Kitchen", "West"): ("22.50", 1, 3, "22.50"),
    (D2, "Stationery", "East"): ("46.96", 2, 6, "23.48"),
    (D2, "Unknown", "South"): ("19.99", 1, 1, "19.99"),  # P011 not in products
}


def gold(database_url: str) -> dict[GoldKey, GoldValue]:
    """Return the gold table keyed by (date, category, region) with values as strings."""
    with psycopg.connect(database_url) as conn:
        rows: list[DailySales] = fetch_gold(conn)
    return {
        (r.order_date, r.category, r.region): (
            str(r.net_revenue),
            r.order_count,
            r.units_sold,
            None if r.aov is None else str(r.aov),
        )
        for r in rows
    }


def scalar(database_url: str, query: str, *params: object) -> object:
    """Run a single-value query and return the value."""
    with psycopg.connect(database_url) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def add_order_file(landing: Path, name: str, *rows: str) -> None:
    """Write an order file with the standard header and the given data rows."""
    (landing / name).write_text(HEADER + "".join(f"{row}\n" for row in rows), encoding="utf-8")


def test_full_load_produces_expected_gold(settings: Settings, database_url: str) -> None:
    """Both drops load into exactly the expected per-day/category/region figures."""
    summary = run(settings)
    assert gold(database_url) == EXPECTED_GOLD
    assert [f.source_file for f in summary.files_loaded] == [
        "orders_2024-01-01.csv",
        "orders_2024-01-02.csv",
    ]
    assert summary.products_reloaded is True
    assert summary.dates_refreshed == (D1, D2)
    assert summary.gold_rows_written == len(EXPECTED_GOLD)


def test_rerun_is_a_no_op(settings: Settings, database_url: str) -> None:
    """Running again with no new files changes nothing and rebuilds nothing."""
    run(settings)
    summary = run(settings)
    assert summary.files_loaded == ()
    assert summary.products_reloaded is False
    assert summary.dates_refreshed == ()
    assert gold(database_url) == EXPECTED_GOLD
    assert scalar(database_url, "SELECT count(*) FROM bronze.orders") == 24


def test_daily_runs_equal_one_batch_load(
    database_url: str,
    landing_dir: Path,
    source_dir: Path,
    reset_db: Callable[[], None],
    tmp_path: Path,
) -> None:
    """Loading one file per day gives the same gold table as loading both at once."""
    settings = Settings(database_url=database_url, data_dir=landing_dir, out_dir=tmp_path)
    shutil.copy(source_dir / "products.csv", landing_dir)
    shutil.copy(source_dir / "orders_2024-01-01.csv", landing_dir)
    run(settings)
    assert gold(database_url)[(D1, "Stationery", "North")] == ("17.50", 1, 5, "17.50")
    shutil.copy(source_dir / "orders_2024-01-02.csv", landing_dir)
    summary = run(settings)
    assert summary.dates_refreshed == (D1, D2)  # day 2 restates day 1
    daily = gold(database_url)

    reset_db()
    run(settings)
    assert daily == gold(database_url) == EXPECTED_GOLD


def test_invalid_rows_are_quarantined_with_reasons(settings: Settings, database_url: str) -> None:
    """Zero-quantity and truncated rows land in quarantine with their raw text."""
    run(settings)
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT source_file, order_id, reason, raw_line FROM silver.orders_quarantine "
            "ORDER BY source_file, line_no"
        ).fetchall()
    assert rows == [
        (
            "orders_2024-01-01.csv",
            "O1008",
            "quantity:zero_quantity",
            "O1008,2024-01-01,C007,P008,0,9.99,North",
        ),
        ("orders_2024-01-02.csv", "O2009", "truncated_row", "O2009,2024-01-02,C019,P004,2,"),
    ]


def test_bronze_keeps_original_line_text(settings: Settings, database_url: str) -> None:
    """Bronze stores the delivered line verbatim, quotes and extra fields included."""
    run(settings)
    assert (
        scalar(
            database_url, "SELECT raw_line FROM bronze.orders WHERE order_id = %s LIMIT 1", "O1002"
        )
        == 'O1002,2024-01-01,C002,P010,1,"$1,099.00",West'
    )
    assert (
        scalar(database_url, "SELECT n_fields FROM bronze.orders WHERE order_id = %s", "O1011") == 8
    )


def test_redelivered_order_latest_file_wins(settings: Settings, database_url: str) -> None:
    """O1003 is corrected to 6 units in the day-2 file and counted once."""
    run(settings)
    assert scalar(database_url, "SELECT quantity FROM silver.orders WHERE order_id = 'O1003'") == 6
    assert (
        scalar(database_url, "SELECT source_file FROM silver.orders WHERE order_id = 'O1003'")
        == "orders_2024-01-02.csv"
    )
    assert scalar(database_url, "SELECT count(*) FROM silver.orders") == 20


def test_stale_backfill_does_not_overwrite_newer_data(
    settings: Settings, database_url: str
) -> None:
    """A late file dated earlier than the current version of an order cannot overwrite it."""
    run(settings)
    add_order_file(
        settings.data_dir, "orders_2023-12-31.csv", "O1003,2024-01-01,C003,P003,99,3.50,North"
    )
    run(settings)
    assert scalar(database_url, "SELECT quantity FROM silver.orders WHERE order_id = 'O1003'") == 6
    assert gold(database_url) == EXPECTED_GOLD


def test_order_moving_date_leaves_its_old_day(settings: Settings, database_url: str) -> None:
    """When a re-delivered order changes date, both the old and new day are rebuilt."""
    run(settings)
    add_order_file(
        settings.data_dir, "orders_2024-01-03.csv", "O2010,2024-01-03,C020,P006,3,7.50,West"
    )
    summary = run(settings)
    assert summary.dates_refreshed == (D2, D3)
    result = gold(database_url)
    assert (D2, "Kitchen", "West") not in result
    assert result[(D3, "Kitchen", "West")] == ("22.50", 1, 3, "22.50")


def test_changed_file_fails_and_rolls_back(settings: Settings, database_url: str) -> None:
    """A loaded file whose content changes fails the run; new files in that run are not loaded."""
    run(settings)
    day1 = settings.data_dir / "orders_2024-01-01.csv"
    day1.write_text(
        day1.read_text(encoding="utf-8") + "O1013,2024-01-01,C1,P001,1,1.00,East\n",
        encoding="utf-8",
    )
    add_order_file(
        settings.data_dir, "orders_2024-01-03.csv", "O3001,2024-01-03,C1,P001,1,24.99,East"
    )
    with pytest.raises(SourceFileChangedError, match=re.escape("orders_2024-01-01.csv")):
        run(settings)
    assert (
        scalar(database_url, "SELECT count(*) FROM ops.load_ledger WHERE file_kind='orders'") == 2
    )
    assert gold(database_url) == EXPECTED_GOLD


def test_header_drift_fails_without_partial_load(settings: Settings, database_url: str) -> None:
    """A file with an unexpected header stops the run before anything is committed."""
    (settings.data_dir / "orders_2024-01-03.csv").write_text(
        HEADER.replace("region", "sales_region"), encoding="utf-8"
    )
    with pytest.raises(SchemaDriftError):
        run(settings)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT to_regclass('gold.daily_sales')").fetchone() == (None,)


def test_product_category_change_restates_history(settings: Settings, database_url: str) -> None:
    """Changing a product's category reloads products and rebuilds every day."""
    run(settings)
    products = settings.data_dir / "products.csv"
    products.write_text(
        products.read_text(encoding="utf-8").replace(
            "P010,Headphones,Electronics", "P010,Headphones,Audio"
        ),
        encoding="utf-8",
    )
    summary = run(settings)
    assert summary.products_reloaded is True
    assert summary.dates_refreshed == (D1, D2)
    result = gold(database_url)
    assert result[(D1, "Audio", "West")] == ("2198.00", 2, 2, "1099.00")
    assert result[(D1, "Electronics", "West")] == ("89.90", 1, 10, "89.90")


def test_bad_and_duplicate_products_are_quarantined(settings: Settings, database_url: str) -> None:
    """Invalid or duplicated product rows are quarantined; the rest still load."""
    products = settings.data_dir / "products.csv"
    products.write_text(
        products.read_text(encoding="utf-8") + "P012,Thing,Kitchen,abc\nP001,Dupe,Kitchen,1.00\n",
        encoding="utf-8",
    )
    run(settings)
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT product_id, reason FROM silver.products_quarantine ORDER BY line_no"
        ).fetchall()
    assert rows == [("P012", "list_price:bad_money"), ("P001", "duplicate_product_id")]
    assert (
        scalar(database_url, "SELECT category FROM silver.products WHERE product_id='P001'")
        == "Electronics"
    )


def test_export_writes_gold_csv(settings: Settings) -> None:
    """The gold table is exported to CSV with a header and an empty AOV for no-order rows."""
    summary = run(settings)
    lines = summary.export_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "order_date,category,region,net_revenue,order_count,units_sold,aov"
    assert "2024-01-01,Electronics,East,74.97,2,3,37.49" in lines
    assert "2024-01-02,Electronics,West,-1099.00,0,-1," in lines
    assert len(lines) == len(EXPECTED_GOLD) + 1


def test_main_exit_codes(settings: Settings) -> None:
    """The CLI exits 0 on success and 1 when a loaded file has changed."""
    argv = [
        "--database-url",
        settings.database_url,
        "--data-dir",
        str(settings.data_dir),
        "--out-dir",
        str(settings.out_dir),
    ]
    assert main(argv) == 0
    day2 = settings.data_dir / "orders_2024-01-02.csv"
    day2.write_text(day2.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert main(argv) == 1


def test_unit_prices_are_exact_decimals(settings: Settings, database_url: str) -> None:
    """Money is stored as NUMERIC, never float."""
    run(settings)
    price = scalar(database_url, "SELECT unit_price FROM silver.orders WHERE order_id='O1002'")
    assert price == Decimal("1099.00")
    assert isinstance(price, Decimal)


def test_reconciliation_detects_gold_drift(settings: Settings, database_url: str) -> None:
    """Gold totals that no longer match silver are caught instead of being published."""
    run(settings)
    with psycopg.connect(database_url) as conn:
        reconcile_gold(conn, {D1, D2})  # consistent after a normal run
        reconcile_gold(conn, set())  # nothing to check
        conn.execute(
            "UPDATE gold.daily_sales SET units_sold = units_sold + 1 WHERE order_date = %s", (D1,)
        )
        with pytest.raises(ReconciliationError, match="units 32 vs 40"):
            reconcile_gold(conn, {D1})


def test_unexpected_file_names_are_ignored(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Files that look like drops but have no valid date in the name are skipped with a warning."""
    add_order_file(settings.data_dir, "orders_2024-13-01.csv", "X1,2024-01-01,C1,P001,1,1.00,East")
    add_order_file(settings.data_dir, "orders_latest.csv", "X2,2024-01-01,C1,P001,1,1.00,East")
    caplog.set_level(logging.WARNING)
    summary = run(settings)
    assert len(summary.files_loaded) == 2
    assert "orders_2024-13-01.csv" in caplog.text
    assert "orders_latest.csv" in caplog.text
