# Latest product search

Latest v1 routes read `latest_offers` joined to the mutable catalogue inside one `REPEATABLE READ` transaction per request. See [latest offers storage](latest-offers-storage.md) for imports, the import epoch and cleanup.

## Request contract

`GET /v1/products/` accepts `q`, comma-separated `chains`, repeated `city`, `brand` and `category`, `city_labels`, `order`, `page_size` and `cursor`. Each repeated value is literal, so a category such as `Voće, povrće` stays one value; chain codes never contain commas. Empty query browses; ordinary text requires two normalized visible characters and at most 256 characters. Barcode and namespaced identity use `/v1/products/{ean}/`, which accepts the same filters. Explicit historical dates on product routes return 400; archives are unchanged.

`page_size` accepts every integer from 1 through 10000, default 100. Deprecated `limit` has the same bounds; conflicting values return 400. Deprecated `fuzzy` remains accepted. Orders are `relevance`, `price_asc` and `price_desc`.

Responses keep product and nested chain fields and add `generation`, `total`, `next_cursor`, `facets`, `outside_location_total`, `complete` and `partial`. `generation` carries the import epoch. Summaries add physical-shop and chain counts, minimum, maximum and average price, minimum and maximum observation dates, normalized unit-price groups and unknown listing status. Optional numeric and date fields may be `null`.

A cursor encodes the epoch, the normalized query, filter, label and order fingerprint, and the exact comparator values of the last returned product, with prices as decimal strings. Page size may change between requests. Errors:

- A malformed cursor or one from another scope or order returns 400.
- A cursor from an older epoch returns 409 with `detail: "cursor_expired"`; the client restarts from page one.
- A search that passes its statement timeout (10 seconds) or total budget (15 seconds) returns 503 with `detail: "search_timeout"`.

Authentication applies as before; cursors confer no additional access.

## Matching

Names and brands use accent-insensitive tokens with decimal pack sizes preserved. Every query token must match. Exact tokens precede prefixes (three or more characters), then bounded edit matches (one edit from four characters, two from eight); adjacent transposition counts as one edit. Explicit numbers and dietary modifiers require exact tokens. Number and unit pairs must match a pack size from the name or the structured quantity. Explicit known brand tokens require that brand, and brand words cannot provide prefix or edit substitutions. A small grocery modifier vocabulary ranks ingredients above processed or flavored products when those modifiers were not requested.

`relevance()` in `service/db/matching.py` is the single definition of these rules. A vocabulary index (`service/db/vocabulary.py`), built once per epoch off the event loop, holds the distinct name, brand and pack variants, a map from each normalized word to its variants, sorted words for prefix lookup and words bucketed by length for edit lookup. For each query token that is not part of a number and unit pair, it collects the variants holding a word that passes the exact, prefix or edit rule; the per-token sets are intersected, and only those candidates are scored with the same rules. The candidate set is a superset of what `relevance()` accepts, so there is no candidate cap and no recall loss; `tests/service/test_vocabulary.py` checks this against an exhaustive `relevance()` loop. An empty query skips scoring.

## Ordering and pagination

Products sort by relevance group (intent, then match quality), then for `price_asc` and `price_desc` by `min_price` in the active scope, then by physical shop count descending, chain count descending and EAN. Each shop contributes its lower valid regular or special price, zero included; `min_price` is the lowest of those. `avg_price` stays in the response as context.

Filters apply to offers before aggregation, with OR within a facet and AND across facets. Each facet counts the relation with every selection except its own. The outside-location count drops only the city selection. Store codes are namespaced by chain. When one shop lists a barcode under two chain codes, the newest price date wins, then the lowest chain product id. `city_labels` maps raw source cities to display labels for city facets; filtering still uses the raw cities, and the mapping is part of the cursor fingerprint.

For a scope it has not seen in the current epoch, the service runs one statement that materializes the filtered relation once and returns every facet, the outside-location count and the ordered product keys. Keys, total and facets are cached per epoch and scope fingerprint in a bounded LRU (64 scopes and 500,000 keys in total; larger scopes are not cached). Every page, the first included, finds its slice by bisecting the cached keys after the cursor and loads product and chain details only for that slice. The catalogue vocabulary and the raw values behind each filter are cached per epoch as well. Matching and catalogue builds run in worker threads; a build interrupted by the time budget still completes for the next request.

## Verification and limits

Integration fixtures exercise the importer, PostgreSQL and authenticated HTTP routes, including corrections, retained branches, duplicate chain codes, repeated literal filters, expired cursors, timeouts, cached later pages and page concatenation beyond 10000 products. The 80 authored relevance fixtures are development judgments, not measured production search quality.

`scripts/benchmark_vocabulary.py` builds the index over N synthetic names and times queries against the exhaustive loop. Its synthetic catalogue has about a thousand distinct words, so common words match far more names than real data would; treat its timings as an upper bound for the matcher alone. National import time, memory, disk and per-page latency remain to be measured on a real archive before deployment.
