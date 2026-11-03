-- Rebuild gold.daily_sales for the order dates in %(dates)s (callers delete those dates first).
-- Orders are joined to products here, not in silver, so a late or corrected product self-heals.
INSERT INTO gold.daily_sales (order_date, category, region, net_revenue, order_count, units_sold, aov)
SELECT o.order_date,
       COALESCE(p.category, 'Unknown')                               AS category,
       o.region,
       SUM(o.quantity * o.unit_price)::NUMERIC(14, 2)                AS net_revenue,
    COUNT(DISTINCT CASE WHEN o.quantity > 0 THEN o.order_id END) AS order_count,
       SUM(o.quantity)                                               AS units_sold,
       ROUND(SUM(o.quantity * o.unit_price)
             / NULLIF(COUNT(DISTINCT CASE WHEN o.quantity > 0 THEN o.order_id END), 0), 2) AS aov
FROM silver.orders AS o
LEFT JOIN silver.products AS p USING (product_id)
WHERE o.order_date = ANY(%(dates)s)
GROUP BY o.order_date, COALESCE(p.category, 'Unknown'), o.region;
