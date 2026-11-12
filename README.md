# Universal Scraper

A selector-based storefront scraper. It builds one listing URL per source, discovers product links with CSS selectors, and upserts the extracted products into MongoDB.

## Run

Install the dependencies with `python -m pip install -r requirements.txt`. Set `MONGODB_URI`, `MONGODB_DATABASE`, and `MONGODB_COLLECTION` in `.env`, then run:

```powershell
python universal_scraper.py
```

Vinted is the active example. To change stores, edit `TARGET` at the top of `universal_scraper.py` and set the desired MongoDB collection. Other example URLs are commented out and need their own selectors before use.

Store CSS profiles live in `scraping/selectors.py`. Set `TARGET["selector_profile"]` to the matching profile key. Progress logs show the current source, listing and product, pacing waits, browser fallbacks, and saved record counts.

To switch sites, comment out the current `TARGET`, uncomment the desired target and its matching profile, and set the desired MongoDB collection. Keep `SELECTOR_PROFILES = {}` active. A profile can select a JSON-LD script with CSS and specify `types` and a dotted `json_path` for fields stored as structured data.

## Target configuration

- `url` is the base listing URL. `query_param` maps each source's `q` to the store's search parameter.
- `sources` is the list of search objects. Each can contain `q`, `gender`, `l_category`, `l_subcategory`, an optional relative `path`, and extra `params`.
- `selectors.product_links` matches product anchors. `selectors.next_page` is optional. `selectors.drop_product_query` can list tracking parameters to drop.
- `selectors.fields` maps output names to CSS selectors. A string reads text. Use `{"css": "...", "attr": "content"}` to read an HTML attribute. Pictures, colors, sizes and payment options collect multiple matches.
- An optional `product_id_pattern` extracts the first capture group from the product URL when the configured selectors have no ID.

Records use the field names configured in `selectors.fields`, including custom names. Fields with no match are omitted. Use `many: True` to collect multiple values and `url: True` to resolve links in custom fields. The product URL is kept for upserts. Extra source metadata requires `metadata_fields`, domain and favicon fields require `derive_brand_identity`, and URL-derived IDs require `product_id_pattern`. MongoDB also stores its ID and timestamps. Upserts leave fields already present in older documents in place.

## Bounded test

```powershell
python universal_scraper.py --max-products 1 --no-mongo
python -m unittest -q
```

The preview prints JSONL to stdout. Normal runs write to the configured MongoDB collection. Page and product limits apply per source. Crawl permission is checked before fetching and rate limits stop the batch when retries are exhausted.

Headless Chromium is used if permitted HTTP markup lacks the configured listing links or product name. Install it with `python -m playwright install chromium --only-shell` if needed, or use `--no-browser` for HTTP only.
