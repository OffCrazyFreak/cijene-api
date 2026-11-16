DROP TABLE IF EXISTS
    generation_retention_holds,
    generation_stats,
    generation_offers,
    generation_chain_products,
    generation_stores,
    generation_products,
    generation_chains,
    publication_state,
    offer_versions,
    chain_product_versions,
    store_versions,
    product_versions,
    chain_versions,
    source_revisions,
    generations
CASCADE;

DROP FUNCTION IF EXISTS reject_version_mutation();
DROP FUNCTION IF EXISTS protect_accepted_generation();
DROP FUNCTION IF EXISTS validate_generation_membership();

CREATE TABLE IF NOT EXISTS latest_offers (
    chain_product_id INTEGER NOT NULL REFERENCES chain_products (id),
    store_id INTEGER NOT NULL REFERENCES stores (id),
    price_date DATE NOT NULL,
    regular_price DECIMAL(10, 2) NOT NULL,
    special_price DECIMAL(10, 2),
    unit_price DECIMAL(10, 2),
    best_price_30 DECIMAL(10, 2),
    anchor_price DECIMAL(10, 2),
    available BOOLEAN,
    anchor_price_date DATE,
    special_sale_type VARCHAR(255),
    PRIMARY KEY (chain_product_id, store_id)
);

CREATE INDEX IF NOT EXISTS idx_latest_offers_store_id ON latest_offers (store_id);

-- An offer older than its chain's newest imported date is a carried-forward observation.
CREATE TABLE IF NOT EXISTS chain_import_dates (
    chain_id INTEGER PRIMARY KEY REFERENCES chains (id),
    price_date DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS import_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    epoch BIGINT NOT NULL DEFAULT 0,
    baseline_complete BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO import_state (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;
ALTER TABLE import_state DROP COLUMN IF EXISTS epoch_transaction;

-- The importer and metadata writers move the epoch themselves once per changing transaction.
-- Row-copying statement triggers kept every batch in memory until a whole-archive import committed.
CREATE OR REPLACE FUNCTION bump_import_epoch_on_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE import_state SET epoch = epoch + 1;
    RETURN NULL;
END;
$$;

DO $triggers$
DECLARE
    served TEXT;
BEGIN
    FOREACH served IN ARRAY ARRAY['chains', 'stores', 'products', 'chain_products', 'latest_offers', 'chain_import_dates'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS epoch_after_insert ON %I', served);
        EXECUTE format('DROP TRIGGER IF EXISTS epoch_after_update ON %I', served);
        EXECUTE format('DROP TRIGGER IF EXISTS epoch_after_delete ON %I', served);
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER epoch_after_truncate AFTER TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION bump_import_epoch_on_truncate()',
            served
        );
    END LOOP;
END
$triggers$;

DROP FUNCTION IF EXISTS bump_import_epoch();
