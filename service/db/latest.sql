SELECT p.ean,p.brand AS product_brand,p.name AS product_name,
       p.quantity AS product_quantity,p.unit AS product_unit,
       c.id AS chain_id,s.id AS store_id,c.code AS chain,s.code AS store_code,s.city,s.address,s.lat,s.lon,s.type,s.zipcode,s.phone,
       cp.code,cp.name,cp.brand,cp.category,cp.quantity,cp.unit,
       o.price_date,o.regular_price,o.special_price,o.unit_price,
       o.available,o.listing_status,o.best_price_30,o.anchor_price
FROM generation_offers m
JOIN offer_versions o USING(version_id)
JOIN generation_chain_products cm ON cm.generation_id=m.generation_id AND cm.id=o.chain_product_id
JOIN chain_product_versions cp ON cp.version_id=cm.version_id
JOIN generation_products pm ON pm.generation_id=m.generation_id AND pm.id=cp.product_id
JOIN product_versions p ON p.version_id=pm.version_id
JOIN generation_stores sm ON sm.generation_id=m.generation_id AND sm.id=o.store_id
JOIN store_versions s ON s.version_id=sm.version_id
JOIN generation_chains cc ON cc.generation_id=m.generation_id AND cc.id=cp.chain_id
JOIN chain_versions c ON c.version_id=cc.version_id
WHERE m.generation_id=$1
