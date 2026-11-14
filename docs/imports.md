# Validated correction imports

Run `python -m service.db.import YYYY-MM-DD/` or `python -m service.db.import YYYY-MM-DD.zip` in the configured service runtime. Input is the existing normalized archive format, with one directory per chain and `stores.csv`, `products.csv` and `prices.csv`. No downloader is included. Required headers follow `crawler/store/output.py`; old inputs may omit `special_price`, `available`, `anchor_price_date` and `special_sale_type`.

The directory or archive name supplies the source date; it does not establish when each shop collected its prices. It must be a real calendar date written YYYY-MM-DD. An anchor date describes the reference price only. Availability accepts empty/unknown, true/false and normalized 1/0. Unknown availability or an absent listing proves nothing about current stock. Sale provenance is retained as supplied.

Validation streams each CSV into a private temporary snapshot and uses a disk-backed SQLite index to check references and duplicate payloads before PostgreSQL mutation. Identical source product/store rows and observations collapse. Conflicting complete source rows or different offers for canonical aliases in one chain/store/date reject the entire input. Price equality alone does not erase contradictory availability, promotion or reference metadata. There is no cheapest-wins rule.

Required regular prices retain zero. Optional zero prices retain the existing convention of NULL. Empty optional prices are NULL; malformed, nonfinite, negative, out-of-range and nonrepresentable currency values fail. Values must fit DECIMAL(10,2) exactly, without database rounding. Metadata has the existing database field-length limits; no unit conversion or quantity-based identity inference is performed.

Chain product identity remains `(chain_id, source code)`. Replays update the existing row's name, brand, category, quantity, unit and global-product link, keeping its ID. Public barcode normalization and namespaced fallback follow the existing common helper, including its restricted-code rules. Corrections do not rewrite independently enriched shared global-product metadata or delete old global products.

The complete validated input, all chains, prices, metadata and statistics commit in one transaction on one connection. Cooperating imports serialize using a transaction advisory lock. A later chain failure or statistics failure rolls back all writes. Pool use outside that import remains unchanged. Corrections replace all observation fields for the same source product/store/date and recompute current chain aggregates and statistics. Replaying does not add duplicate observations. `--skip-stats` safely imports raw rows but deliberately leaves aggregates unchanged until statistics are computed.

## Resource bounds

Named internal constants currently allow 100 chain directories, 2 GiB per CSV and 8 GiB total normalized CSV bytes, with fields up to 64 KiB and write batches of 1,000 rows. There is no total-row truncation. PostgreSQL store/product identity maps still scale with the input/catalogue; validation indexes and snapshots require temporary disk space proportional to input. These are engineering limits, with no national-capacity claim.

The crawler's root-level `archive-info.txt` notice is accepted as a bounded regular file and excluded from chain data. Unknown root files, notice directories and notice symlinks are rejected.

ZIP input allows at most 500 members, the same per-file and total expanded-byte bounds and expansion ratio at most 1,000. All members are checked before extraction. Absolute paths, parent traversal, backslashes, symbolic/special files, duplicate normalized members and paths deeper than chain/file are rejected. Extraction streams into a private temporary directory. Invalid files, headers, row widths and missing references raise explicit errors. Temporary extraction and validation data are removed on success or failure.

## Upgrade and checks

Normal bootstrap transactionally reapplies the schema then the additive numbered `service/db/migrations/001_price_provenance.sql`. It adds nullable `available`, `anchor_price_date` and `special_sale_type` columns to observations. Existing observations remain unknown for these fields. The migration is idempotent and does not remove data.

Run `scripts/test-service.sh` for the frozen Python 3.13 development build and disposable private PostgreSQL 17 tests. An already provisioned disposable server can run `python -m pytest --service-test-dsn=postgresql://postgres@db/postgres`. Never point this test option at production: the fixture creates and drops temporary test databases. Integration checks cover importer correction, replay, invalid input, rollback, provenance, archive safety, schema upgrade and authenticated HTTP.

This split supplies import transactions only. Latest-chain selection and current HTTP response fields are preserved. Missing-branch completeness, immutable publication generations, request/page consistency and previous-generation rollback remain subsequent work. Existing raw-data retention is unchanged. No deployment is part of this change.
