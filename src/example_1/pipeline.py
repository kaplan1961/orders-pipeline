"""Daily orders pipeline: raw CSV drops -> bronze -> silver (+ quarantine) -> gold, on PostgreSQL.

Each run, inside one transaction guarded by an advisory lock:

1. Reload ``products.csv`` into ``silver.products`` if its content hash changed.
2. Discover ``orders_YYYY-MM-DD.csv`` files not yet in ``ops.load_ledger`` (oldest first).
3. For each new file: append raw lines to ``bronze.orders``, validate them into
   ``silver.orders`` (upsert on ``order_id``) or ``silver.orders_quarantine``.
4. Rebuild ``gold.daily_sales`` for every order date touched (all dates if products changed).
5. Reconcile gold totals against silver for those dates; any mismatch fails the run.

Re-running with no new files is a no-op. A file whose content changed after it was loaded
fails the run instead of being silently skipped or double counted.

Run:  uv run orders-pipeline --data-dir data --out-dir out
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL
from pydantic import ValidationError

from example_1.config import Settings
from example_1.db import Conn, apply_schema, connect, read_sql
from example_1.models import (
    DailySales,
    FileLoadResult,
    OrderRecord,
    Product,
    RawLine,
    RunSummary,
    rejection_reason,
)

logger = logging.getLogger(__name__)

ORDER_COLUMNS: Final = (
    "order_id",
    "order_date",
    "customer_id",
    "product_id",
    "quantity",
    "unit_price",
    "region",
)
PRODUCT_COLUMNS: Final = ("product_id", "product_name", "category", "list_price")
ORDER_FILE_RE: Final = re.compile(r"^orders_(\d{4}-\d{2}-\d{2})\.csv$")
PRODUCTS_FILE: Final = "products.csv"
GOLD_EXPORT_FILE: Final = "gold_daily_sales.csv"
PIPELINE_LOCK_ID: Final = 720_240_101
"""Advisory lock key so two scheduled runs can never interleave."""

GOLD_COLUMNS: Final = (
    "order_date",
    "category",
    "region",
    "net_revenue",
    "order_count",
    "units_sold",
    "aov",
)


class PipelineError(Exception):
    """Base class for errors that should fail the run (and roll it back)."""


class SchemaDriftError(PipelineError):
    """A source file's header does not match the expected columns."""


class SourceFileChangedError(PipelineError):
    """A file already in the load ledger has different content than when it was loaded."""


class MissingSourceFileError(PipelineError):
    """A required source file (the product reference) is not in the landing folder."""


class ReconciliationError(PipelineError):
    """Gold totals do not match silver totals for the rebuilt dates."""


class RowRejectedError(PipelineError):
    """A single source row failed validation and must be quarantined.

    Attributes:
        reason: Machine-readable reason stored in the quarantine table.
    """

    def __init__(self, reason: str) -> None:
        """Create the error.

        Args:
            reason: Machine-readable reason, e.g. ``truncated_row``.
        """
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- reading files
def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes.

    Args:
        path: File to hash.

    Returns:
        Hex digest string.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv_lines(path: Path, expected_header: Sequence[str]) -> list[RawLine]:
    """Read a CSV file line by line, keeping each original line next to its parsed fields.

    Lines are parsed individually so the exact delivered text can be stored in bronze. This
    assumes no quoted field spans multiple lines, which holds for the source system.

    Args:
        path: CSV file to read.
        expected_header: Exact column names the first line must contain.

    Returns:
        One RawLine per non-blank data line.

    Raises:
        SchemaDriftError: If the file is empty or the header differs from ``expected_header``.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise SchemaDriftError(f"{path.name}: file is empty")
    header = [column.strip() for column in next(csv.reader([lines[0]]))]
    if header != list(expected_header):
        raise SchemaDriftError(
            f"{path.name}: expected header {list(expected_header)}, got {header}"
        )
    return [
        RawLine(line_no=line_no, raw_line=line, fields=tuple(next(csv.reader([line]))))
        for line_no, line in enumerate(lines[1:], start=2)
        if line.strip()
    ]


def discover_order_files(conn: Conn, data_dir: Path) -> list[tuple[Path, date]]:
    """List daily order files that have not been loaded yet, oldest first.

    Args:
        conn: Open database connection.
        data_dir: Landing folder.

    Returns:
        ``(path, file_date)`` pairs for new files.

    Raises:
        SourceFileChangedError: If an already-loaded file now has different content.
    """
    ledger: dict[str, str] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source_file, sha256 FROM ops.load_ledger WHERE file_kind = 'orders'"
        )
    }
    pending: list[tuple[Path, date]] = []
    for path in sorted(data_dir.glob("orders_*.csv")):
        match = ORDER_FILE_RE.fullmatch(path.name)
        try:
            file_date = date.fromisoformat(match.group(1)) if match else None
        except ValueError:
            file_date = None
        if file_date is None:
            logger.warning("ignoring file with unexpected name: %s", path.name)
            continue
        known = ledger.get(path.name)
        if known is None:
            pending.append((path, file_date))
        elif known != file_sha256(path):
            raise SourceFileChangedError(
                f"{path.name} changed after it was loaded; reprocess it explicitly"
            )
    return pending


# --------------------------------------------------------------------------- validation
def validate_order_line(line: RawLine) -> OrderRecord:
    """Validate one raw order line.

    Lines with fewer fields than the header are rejected as truncated (a missing trailing
    field cannot be told apart from a shifted column). Extra trailing fields are tolerated:
    the first seven fields are used and the full line stays in bronze.

    Args:
        line: The raw line.

    Returns:
        The validated order.

    Raises:
        RowRejectedError: If the line is truncated or any field fails validation.
    """
    if line.n_fields < len(ORDER_COLUMNS):
        raise RowRejectedError("truncated_row")
    values = dict(zip(ORDER_COLUMNS, line.fields, strict=False))
    try:
        return OrderRecord.model_validate(values)
    except ValidationError as exc:
        raise RowRejectedError(rejection_reason(exc)) from exc


def validate_product_line(line: RawLine) -> Product:
    """Validate one raw product reference line.

    Args:
        line: The raw line.

    Returns:
        The validated product.

    Raises:
        RowRejectedError: If the field count is wrong or any field fails validation.
    """
    if line.n_fields != len(PRODUCT_COLUMNS):
        raise RowRejectedError("wrong_field_count")
    try:
        return Product.model_validate(dict(zip(PRODUCT_COLUMNS, line.fields, strict=True)))
    except ValidationError as exc:
        raise RowRejectedError(rejection_reason(exc)) from exc


def _first_field(line: RawLine) -> str | None:
    """Return the stripped first field (the business key) of a line, if any."""
    return (line.fields[0].strip() or None) if line.fields else None


# --------------------------------------------------------------------------- products
def load_products(conn: Conn, path: Path) -> bool:
    """Reload the product snapshot if ``products.csv`` changed since the last load.

    Invalid and duplicate product rows go to ``silver.products_quarantine``; the first
    occurrence of a duplicated ``product_id`` is kept.

    Args:
        conn: Open database connection (inside the run transaction).
        path: Path to ``products.csv``.

    Returns:
        True if the products were reloaded, False if the file was unchanged.

    Raises:
        SchemaDriftError: If the header is unexpected.
    """
    digest = file_sha256(path)
    known = conn.execute(
        "SELECT sha256 FROM ops.load_ledger WHERE source_file = %s", (path.name,)
    ).fetchone()
    if known is not None and known[0] == digest:
        return False

    products: dict[str, Product] = {}
    rejected: list[tuple[RawLine, str]] = []
    for line in read_csv_lines(path, PRODUCT_COLUMNS):
        try:
            product = validate_product_line(line)
        except RowRejectedError as exc:
            rejected.append((line, exc.reason))
            continue
        if product.product_id in products:
            rejected.append((line, "duplicate_product_id"))
            continue
        products[product.product_id] = product

    conn.execute("TRUNCATE silver.products, silver.products_quarantine")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO silver.products (product_id, product_name, category, list_price) "
            "VALUES (%(product_id)s, %(product_name)s, %(category)s, %(list_price)s)",
            [product.model_dump() for product in products.values()],
        )
        cur.executemany(
            "INSERT INTO silver.products_quarantine (line_no, product_id, reason, raw_line) "
            "VALUES (%s, %s, %s, %s)",
            [
                (line.line_no, _first_field(line), reason, line.raw_line)
                for line, reason in rejected
            ],
        )
    conn.execute(
        """
        INSERT INTO ops.load_ledger
            (source_file, file_kind, sha256, rows_read, rows_clean, rows_quarantined)
        VALUES (%s, 'products', %s, %s, %s, %s)
        ON CONFLICT (source_file) DO UPDATE SET
            sha256 = EXCLUDED.sha256, loaded_at = now(), rows_read = EXCLUDED.rows_read,
            rows_clean = EXCLUDED.rows_clean, rows_quarantined = EXCLUDED.rows_quarantined
        """,
        (path.name, digest, len(products) + len(rejected), len(products), len(rejected)),
    )
    if rejected:
        logger.warning("%s: %d product row(s) quarantined", path.name, len(rejected))
    logger.info("reloaded %s: %d products", path.name, len(products))
    return True


# --------------------------------------------------------------------------- orders
def ingest_bronze(conn: Conn, source_file: str, file_date: date, lines: list[RawLine]) -> None:
    """Append every raw line of an order file to ``bronze.orders``.

    Args:
        conn: Open database connection (inside the run transaction).
        source_file: File name.
        file_date: Date from the file name.
        lines: Raw lines read from the file.
    """
    width = len(ORDER_COLUMNS)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO bronze.orders (source_file, file_date, line_no, raw_line, order_id, "
            "order_date, customer_id, product_id, quantity, unit_price, region, n_fields) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    source_file,
                    file_date,
                    line.line_no,
                    line.raw_line,
                    *(line.fields[i] if i < line.n_fields else None for i in range(width)),
                    line.n_fields,
                )
                for line in lines
            ],
        )


def upsert_silver(
    conn: Conn, source_file: str, file_date: date, records: list[tuple[RawLine, OrderRecord]]
) -> set[date]:
    """Upsert validated orders into ``silver.orders`` (latest delivery wins).

    Args:
        conn: Open database connection (inside the run transaction).
        source_file: File the records came from.
        file_date: Date from the file name, used to order deliveries.
        records: ``(raw line, validated order)`` pairs in file order.

    Returns:
        Order dates to rebuild in gold: the new dates plus the previous dates of any
        re-delivered order (an order that moved date must leave its old day).
    """
    if not records:
        return set()
    order_ids = [record.order_id for _, record in records]
    previous = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT order_date FROM silver.orders WHERE order_id = ANY(%s)", (order_ids,)
        )
    }
    with conn.cursor() as cur:
        cur.executemany(
            SQL(read_sql("upsert_silver_order.sql")),
            [
                {
                    **record.model_dump(),
                    "source_file": source_file,
                    "file_date": file_date,
                    "line_no": line.line_no,
                }
                for line, record in records
            ],
        )
    return previous | {record.order_date for _, record in records}


def load_order_file(conn: Conn, path: Path, file_date: date) -> FileLoadResult:
    """Load one daily order file through bronze into silver and record it in the ledger.

    Args:
        conn: Open database connection (inside the run transaction).
        path: Order file to load.
        file_date: Date parsed from the file name.

    Returns:
        Counts and the order dates touched.
    """
    lines = read_csv_lines(path, ORDER_COLUMNS)
    ingest_bronze(conn, path.name, file_date, lines)

    clean: list[tuple[RawLine, OrderRecord]] = []
    rejected: list[tuple[RawLine, str]] = []
    for line in lines:
        try:
            clean.append((line, validate_order_line(line)))
        except RowRejectedError as exc:
            rejected.append((line, exc.reason))
            continue
        if line.n_fields > len(ORDER_COLUMNS):
            logger.warning(
                "%s line %d: %d extra field(s) ignored",
                path.name,
                line.line_no,
                line.n_fields - len(ORDER_COLUMNS),
            )

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO silver.orders_quarantine (source_file, line_no, order_id, reason, "
            "raw_line) VALUES (%s, %s, %s, %s, %s)",
            [
                (path.name, line.line_no, _first_field(line), reason, line.raw_line)
                for line, reason in rejected
            ],
        )
    affected = upsert_silver(conn, path.name, file_date, clean)
    conn.execute(
        "INSERT INTO ops.load_ledger (source_file, file_kind, sha256, rows_read, rows_clean, "
        "rows_quarantined) VALUES (%s, 'orders', %s, %s, %s, %s)",
        (path.name, file_sha256(path), len(lines), len(clean), len(rejected)),
    )
    result = FileLoadResult(
        source_file=path.name,
        rows_read=len(lines),
        rows_clean=len(clean),
        rows_quarantined=len(rejected),
        affected_dates=frozenset(affected),
    )
    logger.info(
        "loaded %s: %d rows, %d clean, %d quarantined",
        result.source_file,
        result.rows_read,
        result.rows_clean,
        result.rows_quarantined,
    )
    return result


# --------------------------------------------------------------------------- gold
def silver_order_dates(conn: Conn) -> set[date]:
    """Return every order date present in ``silver.orders``.

    Args:
        conn: Open database connection.

    Returns:
        Set of order dates.
    """
    return {row[0] for row in conn.execute("SELECT DISTINCT order_date FROM silver.orders")}


def refresh_gold(conn: Conn, dates: set[date]) -> int:
    """Rebuild ``gold.daily_sales`` for the given dates (delete + insert, i.e. replace-where).

    Gold is always derived from silver, never incremented, so restatements cannot double count.

    Args:
        conn: Open database connection (inside the run transaction).
        dates: Order dates to rebuild.

    Returns:
        Number of gold rows written.
    """
    if not dates:
        return 0
    params = {"dates": sorted(dates)}
    conn.execute("DELETE FROM gold.daily_sales WHERE order_date = ANY(%(dates)s)", params)
    return conn.execute(SQL(read_sql("gold_daily_sales.sql")), params).rowcount


def reconcile_gold(conn: Conn, dates: set[date]) -> None:
    """Check that gold's revenue, order and unit totals equal silver's for the given dates.

    This guards the gold SQL itself: a bad join or grouping that duplicates or drops orders
    fails the run (and rolls it back) instead of publishing wrong numbers.

    Args:
        conn: Open database connection (inside the run transaction).
        dates: Order dates that were just rebuilt.

    Raises:
        ReconciliationError: If any total differs.
    """
    if not dates:
        return
    row = conn.execute(read_sql("reconcile_gold.sql"), {"dates": sorted(dates)}).fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns one row
        raise ReconciliationError("reconciliation query returned no row")
    silver_revenue, gold_revenue, silver_orders, gold_orders, silver_units, gold_units = row
    if (silver_revenue, silver_orders, silver_units) != (gold_revenue, gold_orders, gold_units):
        raise ReconciliationError(
            f"silver vs gold mismatch: revenue {silver_revenue} vs {gold_revenue}, "
            f"orders {silver_orders} vs {gold_orders}, units {silver_units} vs {gold_units}"
        )
    logger.debug("reconciled %d date(s): revenue %s", len(dates), gold_revenue)


def fetch_gold(conn: Conn) -> list[DailySales]:
    """Read the whole gold table in a stable order.

    Args:
        conn: Open database connection.

    Returns:
        Gold rows ordered by date, category and region.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT order_date, category, region, net_revenue, order_count, units_sold, aov "
            "FROM gold.daily_sales ORDER BY order_date, category, region"
        )
        return [DailySales.model_validate(row) for row in cur]


def export_gold(rows: list[DailySales], path: Path) -> None:
    """Write gold rows to CSV (the submitted output table).

    Args:
        rows: Gold rows to write.
        path: Destination CSV path; parent folders are created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(GOLD_COLUMNS)
        for row in rows:
            values = row.model_dump()
            writer.writerow(["" if values[c] is None else values[c] for c in GOLD_COLUMNS])


# --------------------------------------------------------------------------- orchestration
def run(settings: Settings) -> RunSummary:
    """Run the pipeline once: load new files and rebuild the affected gold days.

    All database work happens in a single transaction behind an advisory lock, so a failed
    run leaves no partial state and concurrent runs queue instead of interleaving.

    Args:
        settings: Pipeline configuration.

    Returns:
        A summary of what was loaded and rebuilt.

    Raises:
        PipelineError: On a missing product file, schema drift, a changed source file or a
            failed reconciliation (the run is rolled back).
        psycopg.Error: On database failures (the run is rolled back).
    """
    products_path = settings.data_dir / PRODUCTS_FILE
    if not products_path.is_file():
        raise MissingSourceFileError(f"{products_path} not found")
    with connect(settings.database_url) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (PIPELINE_LOCK_ID,))
            apply_schema(conn)
            products_reloaded = load_products(conn, products_path)
            results = tuple(
                load_order_file(conn, path, file_date)
                for path, file_date in discover_order_files(conn, settings.data_dir)
            )
            dates: set[date] = set()
            for result in results:
                dates.update(result.affected_dates)
            if products_reloaded:  # a category change can restate any day
                dates |= silver_order_dates(conn)
            gold_rows = refresh_gold(conn, dates)
            reconcile_gold(conn, dates)
        export_path = settings.out_dir / GOLD_EXPORT_FILE
        export_gold(fetch_gold(conn), export_path)

    if not results:
        logger.info("no new order files")
    logger.info("gold rebuilt for %d date(s); exported to %s", len(dates), export_path)
    return RunSummary(
        files_loaded=results,
        products_reloaded=products_reloaded,
        dates_refreshed=tuple(sorted(dates)),
        gold_rows_written=gold_rows,
        export_path=export_path,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser; unset options fall back to ``PIPELINE_*`` settings.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", help="PostgreSQL URL (env: PIPELINE_DATABASE_URL)")
    parser.add_argument("--data-dir", type=Path, help="landing folder (env: PIPELINE_DATA_DIR)")
    parser.add_argument("--out-dir", type=Path, help="export folder (env: PIPELINE_OUT_DIR)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point used by the ``orders-pipeline`` script.

    Args:
        argv: Arguments (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 on success, 1 on a pipeline error, 2 on a database error.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    overrides: dict[str, Any] = {
        key: value
        for key, value in {
            "database_url": args.database_url,
            "data_dir": args.data_dir,
            "out_dir": args.out_dir,
        }.items()
        if value is not None
    }
    settings = Settings(**overrides)
    try:
        run(settings)
    except PipelineError as exc:
        logger.error("pipeline failed, nothing was committed: %s", exc)
        return 1
    except psycopg.Error as exc:
        logger.error("database error, nothing was committed: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
