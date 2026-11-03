-- Pipeline schema (idempotent). Layers: ops (run bookkeeping), bronze (raw), silver (clean), gold (reporting).

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- One row per source file ever loaded; its hash makes re-runs no-ops and detects changed files.
CREATE TABLE IF NOT EXISTS ops.load_ledger (
    source_file      TEXT        PRIMARY KEY,
    file_kind        TEXT        NOT NULL CHECK (file_kind IN ('orders', 'products')),
    sha256           TEXT        NOT NULL,
    loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_read        INTEGER     NOT NULL CHECK (rows_read >= 0),
    rows_clean       INTEGER     NOT NULL CHECK (rows_clean >= 0),
    rows_quarantined INTEGER     NOT NULL CHECK (rows_quarantined >= 0)
);

-- Every delivered line, untyped, append-only: nothing is lost and any rule can be replayed.
CREATE TABLE IF NOT EXISTS bronze.orders (
    source_file  TEXT        NOT NULL,
    file_date    DATE        NOT NULL,
    line_no      INTEGER     NOT NULL,
    raw_line     TEXT        NOT NULL,
    order_id     TEXT,
    order_date   TEXT,
    customer_id  TEXT,
    product_id   TEXT,
    quantity     TEXT,
    unit_price   TEXT,
    region       TEXT,
    n_fields     INTEGER     NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_file, line_no)
);

-- Typed, validated, one row per order_id; the latest (file_date, line_no) version wins.
CREATE TABLE IF NOT EXISTS silver.orders (
    order_id     TEXT          PRIMARY KEY,
    order_date   DATE          NOT NULL,
    customer_id  TEXT,
    product_id   TEXT          NOT NULL,
    quantity     INTEGER       NOT NULL CHECK (quantity <> 0),
    unit_price   NUMERIC(12,2) NOT NULL CHECK (unit_price > 0),
    region       TEXT          NOT NULL,
    source_file  TEXT          NOT NULL,
    file_date    DATE          NOT NULL,
    line_no      INTEGER       NOT NULL,
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_silver_orders_order_date ON silver.orders (order_date);

CREATE TABLE IF NOT EXISTS silver.orders_quarantine (
    source_file    TEXT        NOT NULL,
    line_no        INTEGER     NOT NULL,
    order_id       TEXT,
    reason         TEXT        NOT NULL,
    raw_line       TEXT        NOT NULL,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_file, line_no)
);

-- Current product snapshot (type 1), fully reloaded whenever products.csv changes.
CREATE TABLE IF NOT EXISTS silver.products (
    product_id   TEXT          PRIMARY KEY,
    product_name TEXT          NOT NULL,
    category     TEXT          NOT NULL,
    list_price   NUMERIC(12,2) NOT NULL CHECK (list_price >= 0)
);

CREATE TABLE IF NOT EXISTS silver.products_quarantine (
    line_no        INTEGER     PRIMARY KEY,
    product_id     TEXT,
    reason         TEXT        NOT NULL,
    raw_line       TEXT        NOT NULL,
    quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Analyst table: one row per order_date x category x region.
CREATE TABLE IF NOT EXISTS gold.daily_sales (
    order_date   DATE          NOT NULL,
    category     TEXT          NOT NULL,
    region       TEXT          NOT NULL,
    net_revenue  NUMERIC(14,2) NOT NULL,
    order_count  INTEGER       NOT NULL CHECK (order_count >= 0),
    units_sold   INTEGER       NOT NULL,
    aov          NUMERIC(14,2),
    refreshed_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (order_date, category, region)
);
