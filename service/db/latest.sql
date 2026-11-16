SELECT p.ean,p.brand AS product_brand,p.name AS product_name,
       p.quantity AS product_quantity,p.unit AS product_unit,
       c.id AS chain_id,s.id AS store_id,c.code AS chain,s.code AS store_code,s.city,s.address,s.lat,s.lon,s.type,s.zipcode,s.phone,
       cp.id AS chain_product_id,cp.code,cp.name,cp.brand,cp.category,cp.quantity,cp.unit,
       o.price_date,o.regular_price,o.special_price,o.unit_price,
       o.available,'unknown' AS listing_status,o.best_price_30,o.anchor_price
FROM latest_offers o
JOIN chain_products cp ON cp.id=o.chain_product_id
JOIN products p ON p.id=cp.product_id
JOIN stores s ON s.id=o.store_id
JOIN chains c ON c.id=cp.chain_id
WHERE TRUE
