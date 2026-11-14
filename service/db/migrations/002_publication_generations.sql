CREATE TABLE IF NOT EXISTS generations (
    id BIGSERIAL PRIMARY KEY,
    parent_id BIGINT REFERENCES generations(id),
    accepted_at TIMESTAMPTZ,
    origin TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    active_id BIGINT REFERENCES generations(id),
    previous_id BIGINT REFERENCES generations(id),
    epoch BIGINT NOT NULL DEFAULT 0,
    baseline_complete BOOLEAN NOT NULL DEFAULT FALSE
);
INSERT INTO publication_state(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS source_revisions (
    id BIGSERIAL PRIMARY KEY,
    chain_code TEXT NOT NULL,
    source_date DATE NOT NULL,
    content_hash TEXT,
    completeness TEXT CHECK (completeness IN ('unknown', 'partial', 'complete')),
    manifest JSONB
);
CREATE TABLE IF NOT EXISTS chain_versions (
    version_id BIGSERIAL PRIMARY KEY,
    id BIGINT NOT NULL,
    code TEXT NOT NULL,
    created_at TIMESTAMPTZ,
    UNIQUE(id, version_id)
);
CREATE TABLE IF NOT EXISTS product_versions (
    version_id BIGSERIAL PRIMARY KEY,
    id BIGINT NOT NULL,
    ean TEXT NOT NULL,
    brand TEXT,
    name TEXT,
    quantity NUMERIC(10,3),
    unit TEXT,
    created_at TIMESTAMPTZ,
    UNIQUE(id, version_id)
);
CREATE TABLE IF NOT EXISTS store_versions (
    version_id BIGSERIAL PRIMARY KEY,
    id BIGINT NOT NULL,
    chain_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    type TEXT,
    address TEXT,
    city TEXT,
    zipcode TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    phone TEXT,
    created_at TIMESTAMPTZ,
    UNIQUE(id, version_id)
);
CREATE TABLE IF NOT EXISTS chain_product_versions (
    version_id BIGSERIAL PRIMARY KEY,
    id BIGINT NOT NULL,
    chain_id INTEGER NOT NULL,
    product_id INTEGER,
    code TEXT NOT NULL,
    brand TEXT,
    name TEXT NOT NULL,
    category TEXT,
    unit TEXT,
    quantity TEXT,
    created_at TIMESTAMPTZ,
    UNIQUE(id, version_id)
);
CREATE TABLE IF NOT EXISTS offer_versions (
    version_id BIGSERIAL PRIMARY KEY,
    id BIGINT NOT NULL,
    chain_product_id INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    price_date DATE NOT NULL,
    regular_price NUMERIC(10,2) NOT NULL,
    special_price NUMERIC(10,2),
    unit_price NUMERIC(10,2),
    best_price_30 NUMERIC(10,2),
    anchor_price NUMERIC(10,2),
    available BOOLEAN,
    anchor_price_date DATE,
    special_sale_type TEXT,
    revision_id BIGINT REFERENCES source_revisions(id),
    listing_status TEXT NOT NULL DEFAULT 'unknown' CHECK (listing_status IN ('unknown',
    'listed',
    'removed')),
    UNIQUE(id, version_id)
);
CREATE TABLE IF NOT EXISTS generation_chains (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    id BIGINT NOT NULL,
    version_id BIGINT NOT NULL,
    PRIMARY KEY(generation_id, id),
    FOREIGN KEY(id, version_id) REFERENCES chain_versions(id, version_id)
);
CREATE TABLE IF NOT EXISTS generation_products (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    id BIGINT NOT NULL,
    version_id BIGINT NOT NULL,
    PRIMARY KEY(generation_id, id),
    FOREIGN KEY(id, version_id) REFERENCES product_versions(id, version_id)
);
CREATE TABLE IF NOT EXISTS generation_stores (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    id BIGINT NOT NULL,
    version_id BIGINT NOT NULL,
    PRIMARY KEY(generation_id, id),
    FOREIGN KEY(id, version_id) REFERENCES store_versions(id, version_id),
    chain_id BIGINT NOT NULL,
    FOREIGN KEY(generation_id, chain_id) REFERENCES generation_chains(generation_id, id)
);
CREATE TABLE IF NOT EXISTS generation_chain_products (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    id BIGINT NOT NULL,
    version_id BIGINT NOT NULL,
    PRIMARY KEY(generation_id, id),
    FOREIGN KEY(id, version_id) REFERENCES chain_product_versions(id, version_id),
    chain_id BIGINT NOT NULL,
    FOREIGN KEY(generation_id, chain_id) REFERENCES generation_chains(generation_id, id),
    product_id BIGINT,
    FOREIGN KEY(generation_id, product_id) REFERENCES generation_products(generation_id, id)
);
CREATE TABLE IF NOT EXISTS generation_offers (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    id BIGINT NOT NULL,
    version_id BIGINT NOT NULL,
    PRIMARY KEY(generation_id, id),
    FOREIGN KEY(id, version_id) REFERENCES offer_versions(id, version_id),
    chain_product_id BIGINT NOT NULL,
    store_id BIGINT NOT NULL,
    UNIQUE(generation_id, chain_product_id, store_id),
    FOREIGN KEY(generation_id, chain_product_id) REFERENCES generation_chain_products(generation_id, id),
    FOREIGN KEY(generation_id, store_id) REFERENCES generation_stores(generation_id, id)
);
CREATE TABLE IF NOT EXISTS generation_stats (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    chain_product_id BIGINT NOT NULL,
    min_price NUMERIC NOT NULL,
    max_price NUMERIC NOT NULL,
    avg_price NUMERIC NOT NULL,
    offer_count BIGINT NOT NULL,
    min_date DATE NOT NULL,
    max_date DATE NOT NULL,
    PRIMARY KEY(generation_id, chain_product_id),
    FOREIGN KEY(generation_id, chain_product_id) REFERENCES generation_chain_products(generation_id, id)
);
CREATE TABLE IF NOT EXISTS generation_retention_holds (
    generation_id BIGINT NOT NULL REFERENCES generations(id),
    holder TEXT NOT NULL,
    PRIMARY KEY(generation_id, holder)
);
CREATE OR REPLACE FUNCTION reject_version_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable publication data';
END;
$$;
CREATE OR REPLACE FUNCTION protect_accepted_generation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'generations' THEN
        IF OLD.accepted_at IS NOT NULL THEN
            RAISE EXCEPTION 'immutable accepted generation';
        END IF;
    ELSE
        PERFORM 1 FROM generations
        WHERE id IN (
            CASE WHEN TG_OP <> 'INSERT' THEN OLD.generation_id END,
            CASE WHEN TG_OP <> 'DELETE' THEN NEW.generation_id END
        ) ORDER BY id FOR UPDATE;
        IF TG_OP <> 'INSERT' THEN
            target := OLD.generation_id;
            IF EXISTS(SELECT 1 FROM generations WHERE id=target AND accepted_at IS NOT NULL) THEN
                RAISE EXCEPTION 'immutable accepted generation membership';
            END IF;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            target := NEW.generation_id;
            IF EXISTS(SELECT 1 FROM generations WHERE id=target AND accepted_at IS NOT NULL) THEN
                RAISE EXCEPTION 'immutable accepted generation membership';
            END IF;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON chain_versions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON product_versions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON store_versions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON chain_product_versions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON offer_versions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER immutable_rows BEFORE UPDATE OR DELETE OR TRUNCATE ON source_revisions
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE UPDATE OR DELETE ON generations
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generations
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_chains
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_chains
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_products
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_products
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_stores
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_stores
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_chain_products
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_chain_products
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_offers
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_offers
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE TRIGGER accepted_rows BEFORE INSERT OR UPDATE OR DELETE ON generation_stats
FOR EACH ROW EXECUTE FUNCTION protect_accepted_generation();
CREATE OR REPLACE TRIGGER immutable_truncate BEFORE TRUNCATE ON generation_stats
FOR EACH STATEMENT EXECUTE FUNCTION reject_version_mutation();
CREATE OR REPLACE FUNCTION validate_generation_membership() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.accepted_at IS NOT NULL AND OLD.accepted_at IS NULL THEN
        IF EXISTS (
            SELECT 1 FROM generation_stores m
            JOIN store_versions v USING(version_id)
            WHERE m.generation_id=NEW.id AND m.chain_id<>v.chain_id
        ) OR EXISTS (
            SELECT 1 FROM generation_chain_products m
            JOIN chain_product_versions v USING(version_id)
            WHERE m.generation_id=NEW.id AND
                (m.chain_id<>v.chain_id OR m.product_id IS DISTINCT FROM v.product_id)
        ) OR EXISTS (
            SELECT 1 FROM generation_offers m
            JOIN offer_versions v USING(version_id)
            JOIN generation_stores s ON s.generation_id=m.generation_id AND s.id=m.store_id
            JOIN generation_chain_products p ON p.generation_id=m.generation_id AND p.id=m.chain_product_id
            WHERE m.generation_id=NEW.id AND
                (m.store_id<>v.store_id OR m.chain_product_id<>v.chain_product_id OR s.chain_id<>p.chain_id)
        ) THEN
            RAISE EXCEPTION 'Generation metadata membership mismatch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE TRIGGER validate_membership BEFORE UPDATE ON generations
FOR EACH ROW EXECUTE FUNCTION validate_generation_membership();
