FROM latest_offers o
JOIN chain_products cp ON cp.id=o.chain_product_id
JOIN products p ON p.id=cp.product_id
JOIN stores s ON s.id=o.store_id
JOIN chains c ON c.id=cp.chain_id
LEFT JOIN chain_import_dates d ON d.chain_id=c.id
