-- Totals for %(dates)s computed independently from silver and from gold. Every order lands in
-- exactly one gold cell and prices are exact to the cent, so all three pairs must be equal.
WITH silver_totals AS (
    SELECT COALESCE(SUM(quantity * unit_price), 0)  AS revenue,
           COUNT(*) FILTER (WHERE quantity > 0)     AS orders,
           COALESCE(SUM(quantity), 0)               AS units
    FROM silver.orders
    WHERE order_date = ANY(%(dates)s)
), gold_totals AS (
    SELECT COALESCE(SUM(net_revenue), 0)  AS revenue,
           COALESCE(SUM(order_count), 0)  AS orders,
           COALESCE(SUM(units_sold), 0)   AS units
    FROM gold.daily_sales
    WHERE order_date = ANY(%(dates)s)
)
SELECT s.revenue, g.revenue, s.orders, g.orders, s.units, g.units
FROM silver_totals AS s CROSS JOIN gold_totals AS g;
