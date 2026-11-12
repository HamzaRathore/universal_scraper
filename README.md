# Universal Scraper

`universal_scraper.py` handles the CLI. The `scraping/` package contains crawl orchestration, Chromium rendering, and product extraction. Shared helpers are in `scraper_utils.py`, and MongoDB persistence is in `db.py`.

Give the scraper a public storefront, collection, or product URL. It discovers pages through sitemaps and links, prioritizes product and pagination URLs, and exports one JSON record per line.

## Quick run

Set the active `TARGET` object and `MAX_PAGES` at the top of `universal_scraper.py`, then run:

```powershell
python universal_scraper.py
```

Each `TARGET` contains a base `url` and a `tags` mapping from label to same-site path or query. An empty value uses the base URL. Query-only values such as `?search_text=boots` preserve other base query parameters. Each tag crawls sequentially with its own page, detail and product limits. The run shares robots rules, pacing, product deduplication, and the configured MongoDB collection. Saved records include the first matching `crawl_tag` and `crawl_url`. A positional URL override runs a single crawl.

`max_pages` limits discovery pages, including the starting URL and any collection or pagination pages followed. The scraper then visits each unique item URL separately. For example, one discovery page with 16 item URLs can result in 16 detail visits. `--max-products` optionally limits the number of saved records.

Use `--max-details 2` to limit the number of detail pages attempted after discovery, even if none of those pages yields a record.

Detail records retain the listing card's displayed price as `price_display` when available. A row marked `detail_status: "card_only"` uses card data because the detail page yielded no extractable record. The summary reports discovery pages, item URLs, detail attempts, and detail pages without data. Output is saved to `products.jsonl` in this folder and to MongoDB when `MONGODB_URI` is configured in `.env`.

For accommodation sites, start with a direct listing URL. The scraper recognizes Schema.org accommodation records and can read public metadata on numeric `/rooms/<id>` pages. Listing records have `record_type: "accommodation"`. Price and availability stay empty when the page does not provide them.

```powershell
python universal_scraper.py "https://www.airbnb.com/rooms/704918236414078030" --max-pages 1 --max-sitemaps 0 --mongo
```

## Setup on Windows

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.browsers'
.\.venv\Scripts\python.exe -m playwright install chromium --only-shell
```

Set `PLAYWRIGHT_BROWSERS_PATH` again when running from a new PowerShell session.

## Scrape a store

Replace the example URL with the store you want to scrape.

```powershell
.\.venv\Scripts\python.exe universal_scraper.py https://store.example/ --max-pages 100 --max-products 500 --output products.jsonl
```

The scraper checks JSON-LD, embedded JSON containing Schema.org products, microdata, and product metadata. Output includes product URL, source URL, extraction method, title, brand, description, SKU, price, currency, availability, images, offers, and missing fields. Prices preserve the source representation and currency.

## Save to MongoDB

Open the local `.env` file and fill in `MONGODB_URI`. If the file is missing, copy `.env.example` to `.env`. The default database is `universal_scraper` and the default collection is `products`. You can change either in `.env`.

```powershell
.\.venv\Scripts\python.exe universal_scraper.py https://store.example/ --mongo --max-pages 30
```

With a configured URI, the normal command writes to MongoDB and exports `products.jsonl`. Use `--no-mongo` for JSONL only or `--mongo` to require MongoDB configuration. Repeat crawls update the same document when its product URL and SKU match. For records without a SKU, the product title is used with the URL.

MongoDB settings in the project `.env` take precedence over matching shell variables. Each run prints its destination as `Saving products to MongoDB: database.collection`.

By default, the crawler stays on the seed host and its www equivalent, checks robots.txt, and uses HTTP before trying Chromium on likely product pages or pages with limited storefront links. Browser rendering can also extract Schema.org product objects from same-host JSON responses.

The crawl summary reports `robots_skipped` when a robots rule disallows a URL and `robots_unavailable` when the rules cannot be verified. For a timeout, check the exact robots URL printed by the scraper using `curl.exe -L --max-time 30`. For a regional redirect, use the storefront's regional URL as the seed.

The crawler uses Protego for wildcard rules and longest-match precedence. A missing robots file (HTTP 404 or 410) allows the crawl to proceed. Timeouts, access errors, and invalid responses leave the policy unverified. Successful policies cache for up to 24 hours under `.robots-cache/`, subject to `Cache-Control`. Use `--no-robots-cache` to recheck the origin. Temporary HTTP failures use bounded exponential backoff with jitter. HTTP 429 respects `Retry-After`; a wait beyond `--max-retry-wait` ends the current crawl. See [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) and the [Protego documentation](https://github.com/scrapy/protego).

Schema.org `ProductGroup` variants can inherit the parent description, brand, and images while keeping their own SKU, URL, and offer. When the markup supplies them, the output includes `product_group_id`, `size`, and `color`. See the [Google ProductGroup guide](https://developers.google.com/search/docs/appearance/structured-data/product-variants).

- Use `--browser always` for storefronts whose inventory appears after JavaScript runs. Add `--wait-selector` when a specific element signals readiness.
- Use `--browser never` for HTTP-only crawling.
- Browser fallback waits up to 12 seconds for listing product links and 5 seconds for detail markup. If a listing stays empty after the initial wait, it scrolls once to trigger lazy loading. Use `--render-wait 20` to allow more time, `--wait-selector` for a custom readiness signal, or increase `--browser-timeout` if the whole worker needs more time.
- If headless Chromium yields no usable product data, the scraper can try installed Google Chrome in headless mode once per run, with a longer wait and up to two scrolls on a listing. Chrome uses the same crawl policy and request restrictions. Access-denied and rate-limited responses stop browser fallback.
- Failed runs can save a PNG under `screenshots/`. When a browser page is available, the PNG shows that page. If the crawl stops before a page can be captured, the PNG shows a locally generated error diagnostic. The CLI prints the path, and `--report` includes it. PNG generation requires installed Chromium.
- Use `--max-sitemaps 0` to discover pages from the seed and its links, for example when you only want one collection.
- Use `--max-pages 1 --max-sitemaps 0` to extract from a single product URL.
- Top-level requests to the same host are spaced by at least 4 seconds by default, with up to 0.5 seconds of jitter. Use `--delay 8` for a slower crawl. A higher robots crawl delay or request-rate interval takes precedence.
- On HTTP 429, the crawler increases spacing for that host and waits at least 15 seconds before retrying. A longer `Retry-After` takes precedence. The default retry wait budget is 60 seconds. Use `--max-retry-wait 120` to allow a longer cooldown.
- Use `--timeout 30` to give each HTTP connection or read up to 30 seconds.
- Use `--max-depth 2` to limit links followed from the seed, which has depth zero.
- Use `--report scrape-report.json` for platform detection, robots status, redirect chains, URL outcomes, methods, timing and categorized failures.
- Use `--ca-bundle path/to/certs.pem` or `SCRAPER_CA_BUNDLE` to add a trusted PEM to the HTTP trust store. Chromium uses its configured trust store.
- Use `--allow-host shop.example.com` when the crawl is authorized on a second hostname. Redirects still check the destination and its robots policy.
- Use `--browser-memory-mb 768` and `--browser-timeout 40` to bound each isolated browser render. A browser crash gets one replacement attempt. Navigations and same-scope API requests are checked, and the browser worker stops protected redirects.

## Stores without product markup

Pass `--selectors rules.json` for a page layout that needs CSS extraction. Each field is read within its own product container, so a missing value stays with the correct product.

```json
{
  "container": ".product-card",
  "fields": {
    "title": ".product-title",
    "url": "a.product-link",
    "price": ".price",
    "images": "img"
  }
}
```

Selector profiles are optional and run when the page has no usable structured product records. Login-gated or blocked stores, custom API payloads, and pagination that requires clicks or scrolling can need additional integration. The CLI writes a fresh output file per run, prints crawl counts to stderr, and returns status 1 if it found zero products.

## Storefront smoke tests

```powershell
python -m tests.verify_storefronts reports/storefront-results.jsonl
```

The report contains one live CLI result per storefront. Each test uses one discovery page, up to two detail attempts, and a one-product output limit. Results include the seed URL, crawl summary, diagnostic log, and a product sample when available. A `product_card` result means the scraper extracted listing data and the detail page did not yield a matching record.

Run `python -m unittest -v` for local regression checks. These tests cover robots policy matching, gzip responses, sitemap detail limits, and product variants.

## Extraction engines

The CLI reuses structured HTML, metadata, selectors and Playwright. It detects Shopify, WooCommerce, Magento, BigCommerce and Salesforce Commerce Cloud from storefront signals. The Shopify Ajax product endpoint and WooCommerce Store API are deterministic fallbacks when detection and robots policy allow the request. Other detected platforms use the generic engines. Use `--no-public-api` to disable automatic endpoint calls. [Shopify Ajax product](https://shopify.dev/docs/api/ajax/reference/product) · [WooCommerce Store API](https://developer.woocommerce.com/docs/apis/store-api/resources-endpoints/products/)

Use `--feed authorized-products.json` to prioritize a locally supplied Schema.org Product JSON document or array, or provide an authorized, in-scope feed URL. For credentialed partner feeds, download the authorized export with the provider's tooling and pass the local file. Revolve advertises an affiliate catalog, Levi's offers an affiliate program, and Michael Kors advertises a live feed for its EU affiliate program. Confirm eligibility and regional coverage with the provider. [Revolve](https://www.revolve.com/affiliate) · [Levi’s](https://www.levi.com/US/en_US/features/affiliate-program) · [Michael Kors EU](https://ui.awin.com/merchant-profile/15215)

Use `--html-file product.html` to import product markup from a local HTML file you are authorized to use. Set `TARGET["url"]` to the original page URL so relative links resolve correctly. The importer uses the same extraction and storage pipeline and makes no storefront requests. A saved category page yields records only if it already includes extractable product data.

For automatic fallback with `python universal_scraper.py`, place an authorized Schema.org JSON catalog at `sources/<hostname-without-www>.json` or saved product HTML at `sources/<hostname-without-www>.html`. For Zara, use `sources/zara.com.json` or `sources/zara.com.html`. After a live crawl yields zero records, the scraper tries the local JSON first and then the local HTML. Automatic HTML import requires the original product canonical URL in the saved file. Local files are supplied separately and the scraper imports them without contacting the storefront.

## Record compatibility

MongoDB keeps the existing collection and URL/SKU-or-title upsert identity. New fields include `originalPrice`, `variants`, `category`, `sourceUrl`, `extractionMethod`, `confidence`, `scrapedAt`, and `platform`. New writes store a numeric `price` when it can be parsed safely, with the original value in `price_raw`. Consumers should accept legacy string prices and new numeric prices. Confidence scores describe the extraction method and are heuristic.

## Upgrade verification

```powershell
python -m tests.verify_storefronts reports/upgrade-live-results.jsonl --baseline-failures
```

See [the baseline audit](reports/AUDIT.md), [the 18-site results table](reports/LIVE_TEST_REPORT.md), and [the structured diagnostics](reports/upgrade-live-results.jsonl).
