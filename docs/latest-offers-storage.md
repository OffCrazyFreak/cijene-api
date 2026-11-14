# Immutable baseline storage

This change adds storage for later publication work. Existing API queries and incoming imports still use the mutable legacy tables. The baseline is an upgrade snapshot, not the current response dataset. There is no publication, rollback command, generation-aware request adapter, ranking change or cursor support in this change. Do not deploy this intermediate storage change as a completed latest-price service.

## Initialization

`PostgresDatabase.create_tables()` obtains transaction advisory lock 73198421 before any schema operation. It reapplies the numbered SQL migrations in order and runs `backfill_legacy(conn)` in that same transaction. PostgreSQL 17 is the tested server version. There is no separate migration-version ledger.

Stop every old service process, importer, enrichment worker and cleanup command before an actual upgrade. Old binaries do not share the new cleanup boundary. Initialization additionally takes table locks on legacy chains, products, stores, chain products and prices during the first snapshot. These locks protect the snapshot from already running legacy mutations; they cannot stop an old process restarting after the upgrade. An upgrade is an operator-controlled offline step, not an authorization to deploy.

A populated legacy database gets one accepted generation with origin `legacy`, no parent, no previous pointer and epoch 1. For each source chain-product/store identity, the greatest retained source date supplies its offer. A newer observation at another store cannot remove an older branch offer. Stable numeric IDs, source codes, global links, directory metadata, coordinates and every observation field remain in the snapshot. All metadata rows are retained, including rows with no latest offer. Users and API keys stay entirely outside publication storage.

Legacy revision hashes and archive completeness are unknown. No source revision is invented. Each baseline offer has a NULL revision reference and unknown listing status. Existing overwritten corrections cannot be recovered; the snapshot records the retained corrected values and does not invent a previous good generation.

An empty database marks baseline initialization complete without accepting a generation. Repeated startup checks that marker and avoids copying mutable imports received after initialization. A failed or cancelled initialization rolls back schema changes, versions, membership and the marker together. Concurrent initialization uses the same writer lock and produces at most one baseline.

## Tables and invariants

`generations` holds draft and accepted descriptors. `publication_state` holds the singleton active/previous pointers, epoch and baseline completion marker. Product, chain, store, chain-product and offer versions preserve typed values. Generation membership references stable IDs and immutable version IDs. Foreign keys keep referenced metadata in the same generation; acceptance validates membership links and store/chain-product chain identity. Versions are independent of mutable legacy rows.

Accepted generations and their membership cannot be updated, deleted or truncated. Version rows and source revision manifests cannot be updated, deleted or truncated. New draft versions can be inserted for future publication. This uses application-owned tables and triggers, not a security boundary against a database owner who disables triggers.

`generation_stats` rebuilds latest per-chain-product count, exact minimum/maximum/average effective price and minimum/maximum observation dates from the mixed-date offer membership. Effective price uses the lower supplied regular or special price, preserving valid zero prices and the existing lower-price rule. A missing special price uses the regular price. Existing historical aggregate tables remain unchanged. `source_revisions` reserves nullable provenance fields; `generation_retention_holds` reserves references for later cursor retention. Physical reclamation is disabled and no retention duration is selected.

The one-time baseline copies retained metadata and one latest offer per identity. Later publication must reuse immutable versions where values and provenance are unchanged. The small integration fixture measures relation/index size and member counts only; it establishes no national disk, latency or concurrency capacity.

## Transactions and cleanup

Import and cleanup transactions use the same advisory writer lock and read-committed isolation. A transaction connection belongs to its creating asyncio task. Database calls made by an inherited child context fail before SQL, including children released after the parent exits. Independent tasks acquire independent pooled connections. Nested writer modes fail. Context tokens reset in `finally`, including cancellation. Importers remain sequential.

Cleanup holds the lock from protection calculation through all date deletions. VACUUM runs after the transaction. With any accepted generation, direct deletion and the CLI fail clearly. Direct counts return an empty deletion plan with a protected-state warning; CLI dry-run reports that all prices are protected. The database rechecks protection under the lock even after the CLI check, preventing an initialization race. No dry-run reports rows as deletable when generation retention forbids deletion.

## Verification and later work

Integration tests use disposable PostgreSQL 17 and cover fresh/legacy/repeated/concurrent initialization, failed and cancelled backfill, full payload preservation, inherited connections, cancellation reset, independent reader visibility, writer locking and import/cleanup ordering. Existing importer and HTTP tests remain the compatibility guard.

Incoming publication, active/previous transitions, rollback, generation-aware reads, complete search and pagination remain later work. Before changing read semantics, publish writers and read adapters together and validate generation retention dependencies. Downgrading to old mutable readers after publication is enabled is unsupported; use a reviewed recovery procedure rather than silently starting an older binary.
