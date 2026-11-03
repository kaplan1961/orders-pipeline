# NOTES

## How to run
```
docker compose up -d     # PostgreSQL 16 (or point PIPELINE_DATABASE_URL at any Postgres 14+)
uv sync
uv run orders-pipeline --data-dir data --out-dir out
```
Output: table `gold.daily_sales`, also exported to `out/gold_daily_sales.csv` (the submitted
output table). Tests: see README (`uv run pytest`, 74 tests, 99% coverage against a real Postgres).

## Design
```
orders_YYYY-MM-DD.csv -> bronze.orders (raw line + text fields + file/line)
                      -> silver.orders (typed, one row per order_id) / silver.orders_quarantine (reason)
products.csv          -> silver.products / silver.products_quarantine
                      -> gold.daily_sales (date x category x region)
ops.load_ledger       -> which files were loaded, their sha256 and row counts
```
- **Daily schedule:** each run lists `orders_*.csv`, skips files already in the ledger and loads
  new ones oldest first. Re-running is a no-op. Loading day by day gives the same result as loading
  everything at once (tested).
- **One transaction per run, behind `pg_advisory_xact_lock`:** a failed run commits nothing, and
  two overlapping scheduled runs queue instead of interleaving.
- **Fail loudly, don't guess:** a missing `products.csv`, a header change (schema drift), an empty
  file, or an already-loaded file whose content changed fails the run (exit code 1). Silently
  skipping a changed file would hide a restatement; reloading it would double count.
- **Bronze** stores the exact delivered line plus untyped fields, so nothing is lost and any rule
  can be replayed: `--rebuild` re-derives silver and gold from bronze in delivery order.
- **Corrections are explicit:** `--reprocess orders_2024-01-01.csv` accepts a corrected
  re-delivery. The old lines move to `bronze.orders_superseded` (kept for audit), the new ones
  replace them, and silver is replayed, so orders the correction removed disappear rather than
  lingering.
- **Silver** validates each row with a Pydantic model (`OrderRecord`). Bad rows go to quarantine with
  a stable `field:reason` code (e.g. `quantity:zero_quantity`). Good rows are upserted on
  `order_id` with `INSERT ... ON CONFLICT DO UPDATE WHERE (file_date, line_no) >= current`, so the
  newest delivery wins and a stale backfill can't overwrite newer data.
- **Gold** is always rebuilt from silver for the affected dates (delete + insert = replace-where),
  never incremented, so restatements can't double count. Affected dates = dates in the new files +
  the previous date of any re-delivered order (an order that moves date leaves its old day). If
  `products.csv` changed, every day is rebuilt, because a category change restates history.
- Products are joined in gold, not silver, so a late-arriving product fixes itself on the next run.
- Money is `NUMERIC` end to end (Python `Decimal`), never float. AOV is rounded **half-up to cents**
  in SQL, so results are deterministic (e.g. 74.97 / 2 = 37.485 -> **37.49**); float arithmetic
  can land on either side of a half cent and give 37.48 on some platforms.
- **Reconciliation:** after each rebuild, gold's revenue, order and unit totals are checked against
  silver's for the same dates; a mismatch (e.g. a bad join duplicating orders) fails the run.

## What the data told me
| Observation | Handling |
|---|---|
| Day-2 file repeats O1002 (identical) and O1003 (qty 5 -> 6) | Key is `order_id`; the latest file wins (`file_date`, then line). Day 1 is therefore restated when day 2 lands. Files are *not* assumed to contain only their own date. |
| Dates in 3 formats (ISO, `MM/DD/YYYY`, epoch) | All parsed to a date. Slashes assumed US month-first (consistent with the file date: `01/02/2024` is in the Jan-2 file). Epoch read as UTC: `1704153600` = 2024-01-02. |
| `$24.99`, `"$1,099.00"` | Currency symbols/commas stripped; amounts with more than 2 decimals are rejected rather than rounded. |
| `west` / `East`; `electronics` / `Electronics` | Region and category normalised to title case. Without this they split into separate groups. |
| Missing `customer_id` (O1004, O2005) | Kept as NULL; customer isn't in the output. |
| `P099`, `P011` not in products | Kept, category `Unknown`, so revenue isn't lost and the gap is visible. Better than dropping rows silently. |
| `quantity = 0` (O1008) | Quarantined (`quantity:zero_quantity`): not a sale. |
| `quantity = -1` (O2004) | Treated as a **return**: reduces net revenue and units but isn't counted as an order, and is booked on the return date (the original sale isn't in the data). A cell with only returns has 0 orders and NULL AOV (Electronics/West on Jan 2). |
| O1011 has an extra trailing field | Header is validated strictly, but extra trailing fields on a row are tolerated (logged); the full line is kept in bronze. Judgement call: assumed it's a stray flag, not a column shift. |
| O2009 truncated (no price, no region) | Quarantined (`truncated_row`); price isn't imputed from `list_price`. |

## Assumptions
- Net revenue = `quantity * unit_price` (price at time of sale, not `list_price`); there are no
  discounts, tax or shipping in the data.
- Order count = distinct `order_id` with positive quantity. **AOV = net revenue / order count**
  within each cell (so returns' negative revenue is included).
- One row per `order_id` (the files have no order-line concept).
- If a re-delivered order is invalid in the newer file, the previously good version is kept and the
  bad row goes to quarantine.
- Duplicate `product_id` rows in `products.csv`: the first is kept and the rest are quarantined.
- No quoted field spans multiple lines (needed to keep the exact raw line in bronze).

## Known limits / with more time
- Alerting: fail or warn when quarantine volume or `Unknown` revenue exceeds a threshold, and
  reconcile bronze row counts against the source system's own totals if it can supply them.
- The product table is type 1 (overwritten), so a category change restates history. Production
  would want SCD2, or the category captured at sale time.
- Bulk loading with `COPY` instead of `executemany`, and gold partitioned by `order_date`.
- Currency, the epoch's timezone and DD/MM vs MM/DD should be confirmed with the source owner.
- Orchestration (Airflow/Databricks Workflows), CI running the Postgres integration tests, and a
  data contract for the file schema.

## If the data or requirements change
- **A business rule changes** (say, AOV should exclude returns): change the SQL or model, update the
  tests, then run `--rebuild`. Bronze makes history re-derivable without re-ingesting files.
- **Upstream re-sends a corrected file:** the run fails loudly, then `--reprocess <file>` accepts it.
- **A new column appears:** the strict header check stops the run. Add the column to the contract
  (`ORDER_COLUMNS`, bronze, the model), then load. This is deliberate: silent schema drift is the
  failure mode that corrupts reports.
- **Late or out-of-order files:** already handled. Files are keyed by order, not by date, and the
  newest delivery wins, so a late file can't overwrite newer data.
- **More than one file per day** (e.g. `orders_2024-01-02_part2.csv`): widen `ORDER_FILE_RE` and
  order deliveries by (file date, file name). The rest of the design is unchanged.
- **Much larger volume:** `COPY` into bronze, set-based validation in SQL, gold partitioned by
  `order_date`, and only the touched partitions rebuilt (which is already the logic). On Databricks,
  see below.

## Databricks / Delta differences
- **Ingest:** Auto Loader (`cloudFiles`) with a checkpoint replaces my ledger and file listing
  (`cloudFiles.allowOverwrites` plus a reprocess job covers corrected files);
  `rescuedDataColumn` captures extra or malformed fields; schema-evolution mode replaces my strict
  header check.
- **Tables:** Delta bronze/silver/gold (medallion) in Unity Catalog instead of Postgres schemas.
- **Silver upsert:** `MERGE INTO silver USING batch ON order_id WHEN MATCHED AND src.file_date >=
  tgt.file_date THEN UPDATE WHEN NOT MATCHED THEN INSERT` (after de-duplicating the batch with a
  window function). This is the same rule as my `ON CONFLICT ... WHERE`.
- **Gold:** `INSERT ... REPLACE WHERE order_date IN (...)` (dynamic partition overwrite), or a
  materialized view / DLT.
- **Quality:** DLT expectations or Delta `CHECK`/`NOT NULL` constraints with `expect_or_drop`
  routing to quarantine, instead of Python/Pydantic validation.
- **Concurrency:** Delta's optimistic concurrency plus a single-run Workflows job replaces the
  advisory lock.
- **Scale:** Spark-native parsing (`try_to_date`, `try_cast`), `OPTIMIZE`/Z-order or liquid
  clustering on `order_date`, and Delta time travel for audit and replay.
