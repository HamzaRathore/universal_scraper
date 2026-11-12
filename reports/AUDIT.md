# Scraper audit

The selected workspace is the existing Python CLI. The user confirmed it as the upgrade target. There is no Node.js package, frontend, HTTP API, or existing job queue here.

## Existing components

- `universal_scraper.py` owns URL normalization, scoped discovery, HTTP, Protego policy checks, structured data, selectors, Chromium fallback, and JSONL.
- `db.py` performs atomic MongoDB upserts keyed by a digest of URL and SKU or title. Keep that identity and add fields without migrating existing documents.
- `verify_storefronts.py` runs bounded live checks. The baseline report has 12 extracted storefronts and 18 failures. The five existing tests cover selected crawler and extraction paths.
- The main bottlenecks are serial slow reads, large HTML trees, and concurrent Chromium memory. One crawler opens one page at a time, but separate crawls can each launch a browser. Browser failures and scraper memory share one process.
- Request retries currently use a fixed delay. Policies are cached only for the current run. The frontier lacks a depth limit and persisted URL outcomes. Product path heuristics can miss sites such as Gap. Reporting is largely counters and human-readable logs.

## Baseline failures

| Sites | Observed root cause |
|---|---|
| Revolve, ASOS, UNIQLO, Gucci, Prada | Robots reads timed out. The crawl stopped before storefront extraction. The reason the remote path failed to respond is unconfirmed. |
| Zara, H&M, adidas, Aritzia | Robots endpoint returned HTTP 403. |
| McQueen | TLS handshake alert. |
| Versace, New Balance | Certificate chain could not be verified with the available trust store. |
| Coach | Robots redirected to `world.coach.com`, outside the seed hostname scope. |
| Mango | Robots endpoint returned HTTP 429. |
| Levi's, Michael Kors | Storefront listing returned HTTP 403. |
| Abercrombie | Chromium target crashed and a Playwright event listener reported `MemoryError`. |
| Gap | The seed fetched, but link heuristics and browser fallback yielded no product URL. |

## Implementation order

1. Keep the current CLI and MongoDB path. Add policy caching, bounded retries, verified TLS configuration, redirect traces and structured diagnostics.
2. Keep structured HTML and selector extraction. Add platform fingerprints, conservative documented API adapters, canonical product discovery, and normalized fields alongside existing fields.
3. Isolate Chromium and add durable local background jobs, cancellation, progress, and same-domain scheduling.
4. Test deterministic behavior locally, rerun the 18 baseline failures, and document any access restrictions or unverified integrations.
