"""Daily retail orders pipeline that loads raw CSV drops into PostgreSQL.

The pipeline follows a medallion layout:

* ``bronze`` keeps every raw line exactly as delivered,
* ``silver`` holds typed, validated, de-duplicated orders and products (plus quarantine),
* ``gold`` holds the analyst-facing ``daily_sales`` table.
"""

__version__ = "0.1.0"
