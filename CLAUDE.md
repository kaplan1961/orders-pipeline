# CLAUDE.md

Daily retail orders pipeline (take-home exercise): raw CSV drops → PostgreSQL bronze/silver/gold →
`gold.daily_sales` (net revenue, order count, units, AOV per day × category × region).

Read first when relevant: `candidate_exercise.md` (the brief and how it is graded),
`data/data_dictionary.md` (input columns), `NOTES.md` (every assumption and design decision).

## Role

Work as a **senior Python developer and professional data analyst**. Correctness of the numbers
comes first, then clarity, then cleverness. State assumptions, never guess silently, and explain
data decisions in plain language. The brief grades *reasoning over completeness*: prefer a small,
well-argued change over a large one, and record the "why" in `NOTES.md`.

## Commands

```bash
uv sync                                   # create/refresh .venv from uv.lock
docker compose up -d                      # local PostgreSQL 16 (dbs: orders, orders_test)
uv run orders-pipeline                    # run once (PIPELINE_* env vars / .env)
uv run orders-pipeline --data-dir data --out-dir out --database-url postgresql://...

uv run ruff format . && uv run ruff check . --fix
uv run mypy src tests
uv run pytest --cov=example_1 --cov-report=term-missing --cov-fail-under=90
```

Integration tests need `PIPELINE_TEST_DATABASE_URL` pointing at a **throwaway** database (the
fixtures `DROP` the pipeline schemas). Without it they are skipped and only unit tests run.
This is a Windows project; in PowerShell set it with
`$env:PIPELINE_TEST_DATABASE_URL="postgresql://pipeline:pipeline@localhost:5432/orders_test"`.

## Architecture

```
src/example_1/
├── config.py     Settings (pydantic-settings, PIPELINE_* env vars, .env)
├── models.py     Pydantic models: RawLine, OrderRecord, Product, DailySales, FileLoadResult, RunSummary
├── parsing.py    parse_order_date / parse_money for messy source values (raise ParseError(code))
├── db.py         connect() (autocommit; callers open explicit transactions), read_sql(), apply_schema()
├── pipeline.py   steps (load_products, load_order_file, refresh_gold, export_gold), run(), CLI main()
└── sql/          schema.sql, upsert_silver_order.sql, gold_daily_sales.sql, reconcile_gold.sql
tests/            test_parsing, test_models, test_pipeline_unit (no DB), test_pipeline (PostgreSQL)
data/             the exercise's source files — read-only inputs
out/              gold_daily_sales.csv — the submitted output table (committed)
```

Flow: `ops.load_ledger` (file + sha256) → `bronze.orders` (verbatim lines) →
`silver.orders` / `silver.orders_quarantine` and `silver.products` / `silver.products_quarantine`
→ `gold.daily_sales` → `out/gold_daily_sales.csv`.

## Pipeline invariants (do not break these)

- **Source files are never edited.** Do not modify, reformat or "fix" anything in `data/`; build
  test inputs in `tmp_path`.
- **Idempotent daily runs.** Only files missing from `ops.load_ledger` are loaded; a re-run is a
  no-op. An already-loaded file whose hash changed raises `SourceFileChangedError` and must never be
  silently skipped or reloaded.
- **One transaction per run** behind `pg_advisory_xact_lock`. Run-level failures raise a
  `PipelineError` subclass (exit 1); database failures exit 2. A failed run leaves no partial state.
- **Bronze is append-only** and stores each line exactly as delivered.
- **Silver has one row per `order_id`.** The latest `(file_date, line_no)` wins, and a stale or
  backfilled older file can never overwrite newer data.
- **Bad rows are quarantined, never dropped or imputed**, with a stable `field:code` reason
  (e.g. `quantity:zero_quantity`, `truncated_row`).
- **Gold is always rebuilt from silver** for the affected dates (delete + insert), never
  incremented. Affected dates include an order's previous date when it moves. If `products.csv`
  changes, every date is rebuilt.
- **Every run reconciles gold against silver** (revenue, orders, units) for the rebuilt dates;
  a mismatch raises `ReconciliationError`.
- **Products are joined in gold, not silver**; unmatched products report as `Unknown`.
- **Money is exact:** `Decimal` in Python and `NUMERIC` in SQL, never `float`. Rounding is
  half-up to cents, done in SQL.

Business rules (documented in NOTES.md): net revenue = `quantity × unit_price`; a negative
quantity is a return (it reduces revenue and units but is not an order); order count = distinct
`order_id` with quantity > 0; AOV = net revenue / order count, NULL when the count is 0; slash
dates are `MM/DD/YYYY`; epoch timestamps are UTC.

**Changing a business rule is a three-place change:** update `NOTES.md`, `EXPECTED_GOLD` in
`tests/test_pipeline.py` (hand-computed values, never copied from output), and regenerate
`out/gold_daily_sales.csv` by running the pipeline on an empty database.

## Non-negotiable rules

1. **Pydantic v2** for every data model, setting, validation and structured result. Validate
   external data at the boundary with `model_validate`. No bare `dict`s crossing function boundaries.
2. **Tests in the same change** for every new or changed function, class and SQL rule.
3. **Google-style docstrings** on every module, class, function and method (`Args`, `Returns`,
   `Raises`, `Example` where useful). Docstrings explain *what* and *why*.
4. **Formatted, linted and typed:** Ruff format + lint (config in `pyproject.toml`) and
   `mypy --strict` must pass.
5. **uv only:** `uv add` / `uv add --dev` / `uv remove`, run tools with `uv run`. Never use
   pip/poetry/venv, never hand-edit dependency lists, and always commit `uv.lock`.

## Conventions

**Pydantic.** Follow `OrderRecord` in `models.py`: `ConfigDict(frozen=True, extra="forbid")`,
`Field` constraints, `mode="before"` validators that normalise text, and `PydanticCustomError`
with a snake_case code so `rejection_reason()` stays stable. Configuration only via `Settings`,
never `os.environ` (tests are the exception).

**SQL.** Keep queries in `src/example_1/sql/*.sql` (shipped with the package) or as short
literals in `pipeline.py`. Use bound parameters only (`%s` / `%(name)s`), never f-strings or
`.format()`. Use schema-qualified table names. Schema changes go in `schema.sql` and must be
idempotent (`IF NOT EXISTS`) with constraints (`NOT NULL`, `CHECK`, primary keys).

**Python.** Python 3.13 (`.python-version`), line length 100, full type hints, modern syntax
(`X | None`, `type` aliases), `pathlib`, module-level `logger = logging.getLogger(__name__)` with
`%`-style arguments, and no `print`. Raise specific exceptions; never use a bare `except`. Keep
functions small and pure where possible.

**Tests.** Use pytest with `parametrize` for input variations; shared fixtures live in
`tests/conftest.py`. Name tests by behaviour (`test_stale_backfill_does_not_overwrite_newer_data`).
Cover the happy path, edge cases and every rejection rule. Database behaviour is tested against
real PostgreSQL, not mocks. Coverage must stay ≥ 90%.

**Analysis.** Before changing logic, check every number against the raw CSVs. Report findings as:
what was measured, the rule applied, the result and the caveats. Results must be deterministic:
stable ordering and exact decimals.

## Gotchas

- CSV lines are parsed one at a time to keep the raw text, so quoted fields must not span lines.
- `products.csv` is a full snapshot reloaded only when its hash changes. Its quarantine table is
  rebuilt along with it.
- The `Settings` default URL matches `docker-compose.yml` and is for local dev only. Put real
  credentials in `.env`, which is gitignored.
- If mypy reports an INTERNAL ERROR, the `.mypy_cache` is stale: run with `--cache-dir` pointing
  elsewhere, or delete the cache.

## Definition of done

- [ ] Ruff format/check, `mypy --strict src tests` and pytest (≥ 90% coverage, DB tests *not*
      skipped) all pass.
- [ ] New or changed code has docstrings, type hints and Pydantic models where data crosses a boundary.
- [ ] Pipeline invariants still hold; any business-rule change is reflected in NOTES.md, the tests
      and `out/gold_daily_sales.csv`.
- [ ] Dependencies changed only via uv, with `uv.lock` committed.
- [ ] README/NOTES updated if commands, setup or behaviour changed.
