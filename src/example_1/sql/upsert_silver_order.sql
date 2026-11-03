-- Upsert one validated order. The newest delivery wins: a later file (or a later line in the same
-- file) overwrites, while a stale/backfilled older file can never overwrite newer data.
INSERT INTO silver.orders AS t (
    order_id, order_date, customer_id, product_id, quantity, unit_price, region,
    source_file, file_date, line_no
) VALUES (
    %(order_id)s, %(order_date)s, %(customer_id)s, %(product_id)s, %(quantity)s, %(unit_price)s,
    %(region)s, %(source_file)s, %(file_date)s, %(line_no)s
)
ON CONFLICT (order_id) DO UPDATE SET
    order_date  = EXCLUDED.order_date,
    customer_id = EXCLUDED.customer_id,
    product_id  = EXCLUDED.product_id,
    quantity    = EXCLUDED.quantity,
    unit_price  = EXCLUDED.unit_price,
    region      = EXCLUDED.region,
    source_file = EXCLUDED.source_file,
    file_date   = EXCLUDED.file_date,
    line_no     = EXCLUDED.line_no,
    updated_at  = now()
WHERE (EXCLUDED.file_date, EXCLUDED.line_no) >= (t.file_date, t.line_no);
