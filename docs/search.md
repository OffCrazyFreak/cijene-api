# Latest product search

Latest v1 routes read accepted immutable publication generations. Imports and enrichment publish from the validated workspace inside the serialized import transaction. A failed publication rolls back workspace changes and the active pointer together. Unchanged replay keeps the active generation. Older imports retain newer offer observations and metadata; missing branches retain dated latest-known observations. Their listing status remains unknown.

## Request contract

`GET /v1/products/` accepts `q`, comma-separated `chains`, `city`, `brand`, `category`, `order`, `page_size` and `cursor`. Empty query browses; ordinary text requires two normalized visible characters. Barcode and namespaced identity use `/v1/products/{ean}/`. Explicit historical dates on product routes return 400; archives are unchanged.

`page_size` accepts every integer from 1 through 10000, default 100. Deprecated `limit` has the same bounds; conflicting values return 400. Deprecated `fuzzy` remains accepted; both values use the complete matcher. Orders are `relevance`, `price_asc` and `price_desc`. Price ordering uses the scoped average within relevance groups.

Responses preserve product and nested chain fields and add `generation`, `total`, `next_cursor`, `facets`, `outside_location_total`, `complete` and `partial`. Summaries add physical-shop and chain counts, minimum/maximum observation dates, normalized unit-price groups and unknown listing status. The scalar `price_date` is the latest observation date. Partial evidence describes source knowledge, while complete describes evaluation of the accepted matching relation.

A cursor encodes the retained generation, normalized query/filter/order fingerprint and exact comparator values. Prices in comparator keys are decimal strings. Invalid cursors, changed scope/order or unavailable generations return an explicit restart/error response. Page size may change between requests. Authentication applies as before; cursors confer no additional access.

## Matching and ordering

Names and brands use accent-insensitive tokens with decimal pack sizes preserved. Every query token must match. Exact tokens precede prefixes, then bounded edit matches; adjacent transposition counts as one edit. Explicit numbers and dietary modifiers require exact tokens. The matcher examines every eligible published name, with no candidate-count or trigram-recall cutoff.

Within text quality groups, a small grocery modifier vocabulary favors ingredients over processed/flavored products when those modifiers were not requested. Explicit modifier queries preserve that intent. Coverage then orders distinct physical shops descending, distinct chains descending and canonical identity ascending. Product name and length never break coverage ties.

Filters apply to actual offers before aggregation and pagination, with OR within a facet and AND across facets. Each facet excludes its own current selection. Outside-location counts exclude only locality. Store codes are namespaced by chain; address similarity never merges shops. Canonical aliases in one shop are deduplicated, and conflicting observations reject explicitly rather than selecting the cheapest alias.

Each shop contributes one lower valid regular/special price, including zero. Minimum, maximum and average use that same scoped relation. Unit prices normalize each shop before averaging, with mass, volume and pieces separate; unsupported quantities remain unknown. No stock or removal claim follows from absence in an import.

## Verification and limits

Integration fixtures exercise the actual importer, PostgreSQL and authenticated HTTP routes, including correction, failed publication, retained branches and generation-pinned page concatenation beyond 10000 candidates. The 80 authored relevance fixtures are development judgments, not measured production search quality. Two identity fixtures use symbolic candidate IDs without actual barcodes and cannot establish barcode matching quality; real HTTP identity tests cover canonical lookup separately. Fresh versus preserved vegetables and processed-food preferences remain authored intent judgments.

This first implementation reads the complete immutable offer relation into the service for exhaustive matching and aggregation. It favors recall and transparent correctness over bounded national-scale memory or latency. National-scale concurrency, import overlap and production capacity remain unverified and require measurements before deployment. No candidate truncation hides that limitation. Manual recovery, reclamation and new search engines remain deferred.
