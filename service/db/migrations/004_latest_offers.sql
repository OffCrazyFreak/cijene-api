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

CREATE TABLE IF NOT EXISTS import_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    epoch BIGINT NOT NULL DEFAULT 0,
    epoch_transaction XID8,
    baseline_complete BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO import_state (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;

-- One bump per writing transaction keeps cursors valid across batches of the same import.
CREATE OR REPLACE FUNCTION bump_import_epoch() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    changed BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        changed := EXISTS (SELECT 1 FROM new_rows);
    ELSIF TG_OP = 'DELETE' THEN
        changed := EXISTS (SELECT 1 FROM old_rows);
    ELSIF TG_OP = 'TRUNCATE' THEN
        changed := TRUE;
    ELSE
        -- Provenance dates are not served, so stamping them must not expire cursors.
        changed := EXISTS (
            SELECT to_jsonb(n) - 'metadata_source_date' FROM new_rows n
            EXCEPT
            SELECT to_jsonb(o) - 'metadata_source_date' FROM old_rows o
        );
    END IF;

    IF changed THEN
        UPDATE import_state
        SET epoch = epoch + 1, epoch_transaction = pg_current_xact_id()
        WHERE epoch_transaction IS DISTINCT FROM pg_current_xact_id();
    END IF;

    RETURN NULL;
END;
$$;

DO $triggers$
DECLARE
    served TEXT;
BEGIN
    FOREACH served IN ARRAY ARRAY['chains', 'stores', 'products', 'chain_products', 'latest_offers'] LOOP
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER epoch_after_insert AFTER INSERT ON %I '
            'REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_import_epoch()',
            served
        );
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER epoch_after_update AFTER UPDATE ON %I '
            'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_import_epoch()',
            served
        );
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER epoch_after_delete AFTER DELETE ON %I '
            'REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION bump_import_epoch()',
            served
        );
        EXECUTE format(
            'CREATE OR REPLACE TRIGGER epoch_after_truncate AFTER TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION bump_import_epoch()',
            served
        );
    END LOOP;
END
$triggers$;
