# Latest offers storage

Latest v1 routes read one mutable table, `latest_offers`, joined to the mutable catalogue tables (`chains`, `stores`, `products`, `chain_products`). The dated `prices` table stays as history for the legacy v0 routes and statistics. An earlier design kept immutable generations, versions and membership rows for every import; migration 004 removes it because each daily import copied nearly every national offer while only about 0.8% of regular prices changed between two sampled days.

## Tables

`latest_offers` holds one row per chain product and store, keyed by `(chain_product_id, store_id)`, with the same observation columns as `prices`: price date, regular and special price, unit price, best price in 30 days, anchor price and date, availability and special sale type. A store id index serves store-scoped reads.

`chain_import_dates` holds each chain's newest imported price date. An offer older than that date is a carried-forward observation, which search reports as `partial`.

`import_state` is a single row with the import `epoch` and a `baseline_complete` marker. The epoch is the version that cursors and in-process search caches compare against.

## Imports

An import runs in one serialized writer transaction (advisory lock 73198421). Each price batch is inserted into `prices` as before, and the same batch is upserted into `latest_offers`. Batches pass their columns as arrays to `unnest` instead of a temp table, because the whole archive is one transaction and every dropped temp table would keep its lock until commit. When one batch holds two dates for the same chain product and store, only the newest goes to `latest_offers`; the dated history keeps both. The upsert replaces an existing offer only when the incoming price date is newer or equal, so same-date corrections replace and an older archive never overrides a newer offer. Unchanged rows are skipped. A shop that is missing from a newer file keeps its latest known offer with its own date; absence is not treated as removal and listing status stays `unknown`.

Store and chain product upserts also skip unchanged rows. Newer metadata still wins over older imports: `metadata_source_date` on stores and chain products records the archive date that supplied them, and an older import restores newer metadata before it commits.

When two chain codes of one EAN have offers in the same shop (for example after a chain moves an EAN to a new code, or lists one wine under `0725765718716` and `725765718716`), validation keeps both. Each batch looks up the other codes of its own barcodes in the same shops, and the import logs the distinct shop count once and continues. Reads pick the offer with the newest price date, then the lowest chain product id.

## Epoch

The service moves the epoch itself. Every write helper that touches `chains`, `stores`, `products`, `chain_products`, `latest_offers` or `chain_import_dates` reports how many rows it really changed; the upserts skip unchanged rows, so identical replays report none. Inside an import or metadata writer transaction the epoch moves once, just before commit, if any helper reported a change. A helper called on its own moves it in its own transaction. Stamping `metadata_source_date` never moves it. A `TRUNCATE` of a served table moves it through a plain statement trigger. Other manual SQL on served tables does not move the epoch, so run `UPDATE import_state SET epoch = epoch + 1` in the same transaction after such a change.

The epoch used to come from statement triggers with transition tables. Inside one whole-archive import, PostgreSQL kept every statement's transition rows until commit, and the national 2026-11-14 archive drove the database backend past 2 GiB. Writes inside an import transaction also no longer open a savepoint per batch, because any error already rolls back the whole import.

Readers open a `REPEATABLE READ` transaction, so a request sees one consistent snapshot of the catalogue, the offers and the epoch, and an import that commits during the request is invisible to it. A cursor issued under an older epoch is rejected with HTTP 409 `cursor_expired`, and the client restarts from page one.

## Initialization

`PostgresDatabase.create_tables()` takes the writer lock, reapplies the schema and every numbered migration in order, then runs the one-time backfill in the same transaction. When `baseline_complete` is false, the backfill copies each chain product and store's latest dated row from `prices` into `latest_offers`, seeds `chain_import_dates` and sets the marker. Legacy rows stored a missing special price as 0.00; the copy turns those into NULL so they cannot win a lowest-price sort. A fresh database sets the marker with nothing to copy. A failed or cancelled initialization rolls back the tables, the copy and the marker together, and concurrent initializations produce one copy.

There is no migration ledger: every startup reapplies 001 to 004. Migration 002 therefore recreates the old generation tables and 004 drops them again on each start. This is harmless but wasteful; a ledger is a possible follow-up.

## Cleanup

`service.db.clean_prices` deletes dated `prices` rows older than the kept number of days again, since latest offers no longer depend on them. Each chain's latest date stays. Cleanup never touches `latest_offers`, so a shop's last known offer survives the deletion of its dated row. Cleanup holds a session writer lock across separate per-date transactions, keeps completed dates after an interruption and runs VACUUM after releasing the lock.

## Verification

Integration tests on disposable PostgreSQL 17 cover backfill, repeated and concurrent initialization, failed and cancelled backfill, full payload copy, correction, replay, failed imports, older archives, retained branches, duplicate chain codes, direct metadata updates, epoch moves without row-copying triggers or savepoints and cleanup with retained latest offers. National-scale import time, disk use and latency remain unmeasured.
