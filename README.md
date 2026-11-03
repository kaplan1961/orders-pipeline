# Daily Orders Pipeline

[![CI](https://github.com/kaplan1961/orders-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/kaplan1961/orders-pipeline/actions/workflows/ci.yml)

Loads the retail order system's daily CSV drops and the product reference into PostgreSQL and
builds `gold.daily_sales`: net revenue, order count, units sold and average order value per
**day × product category × region**.

```
data/orders_YYYY-MM-DD.csv ─► bronze.orders ─► silver.orders ──────┐
                                   └────────► silver.orders_quarantine
data/products.csv ─────────────────────────► silver.products ──────┴─► gold.daily_sales ─► out/gold_daily_sales.csv
```

Design decisions, data findings and trade-offs are in [NOTES.md](NOTES.md).

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs Python 3.13 and all dependencies)
- PostgreSQL 14+; `docker compose up -d` starts one locally

## Setup

```bash
docker compose up -d          # PostgreSQL 16 on localhost:5432 (db "orders" + "orders_test")
uv sync                       # create .venv from uv.lock
cp .env.example .env          # optional; defaults already match docker-compose
```

## Run

```bash
uv run orders-pipeline                      # uses PIPELINE_* settings / .env
uv run orders-pipeline --data-dir data --out-dir out --database-url postgresql://...
uv run orders-pipeline --reprocess orders_2024-01-01.csv   # accept a corrected re-delivery
uv run orders-pipeline --rebuild                           # re-derive silver/gold after a rule change
```

Schedule it once a day after the new file lands. Each run loads only files not yet in
`ops.load_ledger`, so re-running is safe. If an already-loaded file changes, the run fails until
the correction is accepted with `--reprocess`. Exit codes: `0` success, `1` pipeline error (schema drift,
changed source file, failed reconciliation), `2` database error. A failed run commits nothing.

Query the result:

```sql
SELECT * FROM gold.daily_sales ORDER BY order_date, category, region;
```

## Development

```bash
uv run ruff format .
uv run ruff check . --fix
uv run mypy src tests
# Integration tests need a throwaway database (they DROP the pipeline schemas):
PIPELINE_TEST_DATABASE_URL=postgresql://pipeline:pipeline@localhost:5432/orders_test \
  uv run pytest --cov=example_1 --cov-report=term-missing --cov-fail-under=90
```

PowerShell: `$env:PIPELINE_TEST_DATABASE_URL="postgresql://pipeline:pipeline@localhost:5432/orders_test"; uv run pytest --cov=example_1 --cov-fail-under=90`

CI (`.github/workflows/ci.yml`) runs all of the above against PostgreSQL 16 on every push, and
checks that `out/gold_daily_sales.csv` matches a fresh pipeline run.

Without `PIPELINE_TEST_DATABASE_URL` the database tests are skipped and the unit tests still run.

## Layout

```
src/example_1/
├── config.py      # Pydantic settings (PIPELINE_* env vars)
├── models.py      # Pydantic models: RawLine, OrderRecord, Product, DailySales, run results
├── parsing.py     # date / money parsers for the messy source values
├── db.py          # connection + packaged SQL access
├── pipeline.py    # bronze -> silver -> gold steps, orchestration, CLI
└── sql/           # schema.sql, upsert_silver_order.sql, gold_daily_sales.sql, reconcile_gold.sql
tests/             # unit tests + PostgreSQL integration tests
```
