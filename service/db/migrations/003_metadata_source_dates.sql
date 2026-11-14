ALTER TABLE stores ADD COLUMN IF NOT EXISTS metadata_source_date DATE;
ALTER TABLE chain_products ADD COLUMN IF NOT EXISTS metadata_source_date DATE;
ALTER TABLE store_versions ADD COLUMN IF NOT EXISTS metadata_source_date DATE;
ALTER TABLE chain_product_versions ADD COLUMN IF NOT EXISTS metadata_source_date DATE;
