"""Scrape tagged storefront listings with configured CSS selectors."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DEPS = PROJECT_DIR / ".deps"
LOCAL_BROWSERS = PROJECT_DIR / ".browsers"

if LOCAL_DEPS.is_dir():
    sys.path.insert(0, str(LOCAL_DEPS))


from db import StorageError, connect
from scraper_utils import progress, web_url
from scraping.crawler import StoreCrawler
from scraping.extraction import source_url
from scraping.selectors import SELECTOR_PROFILES


# Enable only one TARGET and uncomment its profile in scraping/selectors.py.
# TARGET = {
#     "url": "https://pk.khaadi.com/ready-to-wear/chapter-2/kurta-/",
#     "brand": "khaadi", "selector_profile": "khaadi",
#     "product_id_pattern": r"/([^/]+)\.html$",
#     "sources": [{"q": "Chapter 2 Kurtas", "gender": "women", "l_category": "ready to wear", "l_subcategory": "kurta"}],
# }
# TARGET = {
#     "url": "https://www.breakout.com.pk/collections/men-jacket",
#     "brand": "breakout", "selector_profile": "breakout",
#     "product_id_pattern": r"/products/([^/]+)$",
#     "sources": [{"q": "Jackets / Coats", "gender": "men", "l_category": "clothing", "l_subcategory": "jackets"}],
# }
# TARGET = {
#     "url": "https://www.alexandermcqueen.com/en-us/ca/women/shoes",
#     "brand": "alexandermcqueen", "selector_profile": "alexandermcqueen",
#     "product_id_pattern": r"-([A-Za-z0-9]+)\.html$",
#     "sources": [{"q": "Shoes", "gender": "women", "l_category": "shoes"}],
# }
# TARGET = {
#     "url": "https://pk.lamaretail.com/collections/woman-boots",
#     "brand": "lama", "selector_profile": "lama",
#     "product_id_pattern": r"/products/([^/]+)$",
#     "sources": [{"q": "Boots", "gender": "women", "l_category": "shoes", "l_subcategory": "boots"}],
# }
TARGET = {
    "url": "https://www.airbnb.com/homes",
    "brand": "airbnb", "selector_profile": "airbnb",
    "product_id_pattern": r"/rooms/(\d+)",
    "sources": [{"q": "Islamabad", "path": "/islamabad-pakistan/stays", "l_category": "stays"}],
}
# TARGET = {
#     "url": "https://www.vinted.co.uk/catalog/1231-shoes",
#     "brand": "vinted",
#     "query_param": "search_text",
#     "product_id_pattern": r"/items/(\d+)(?:-|/|$)",
#     "sources": [
#         {"q": "boots", "path": "/catalog/1233-men_boots", "gender": "men",
#          "l_category": "shoes", "l_subcategory": "boots"},
#         {"q": "trainers", "path": "/catalog/1242-men_sneakers", "gender": "men",
#          "l_category": "shoes", "l_subcategory": "trainers"},
#     ],
#     "selector_profile": "vinted",
# }
MAX_PAGES = 1



def main():
    import soupsieve

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-products", type=int, help="Limit saved products per source")
    parser.add_argument("--delay", type=float, default=4)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--max-retry-wait", type=float, default=60)
    parser.add_argument("--no-mongo", action="store_true", help="Preview records as JSONL without writing to MongoDB")
    parser.add_argument("--no-browser", dest="browser", action="store_false")
    options = parser.parse_args()
    options.robots_cache = PROJECT_DIR / ".robots-cache"
    options.ca_bundle = os.getenv("SCRAPER_CA_BUNDLE")
    if (options.max_pages < 1 or (options.max_products is not None and options.max_products < 1)
            or not 0 <= options.delay < float("inf") or not 0 < options.timeout < float("inf")
            or not 0 <= options.retries <= 5 or not 0 <= options.max_retry_wait < float("inf")):
        parser.error("Check crawl limits, delay, timeout and retry settings.")
    try:
        target = {**TARGET, "url": web_url("", TARGET["url"], clean=True)}
        if not target["url"]:
            raise ValueError("Provide a full storefront URL.")
        profile = target["selector_profile"]
        if profile not in SELECTOR_PROFILES:
            raise ValueError(f"No CSS selector profile configured for {profile!r}.")
        selectors = SELECTOR_PROFILES[profile]
        target["selectors"] = selectors
        fields = selectors["fields"]
        if not fields or any(not key or "." in key or key.startswith("$") or key in {"_id", "created_at", "updated_at"}
                             for key in fields):
            raise ValueError("Provide field selectors with valid MongoDB field names.")
        soupsieve.compile(selectors["product_links"])
        if structured := selectors.get("structured_links"):
            soupsieve.compile(structured["css"])
        if selectors.get("next_page"):
            soupsieve.compile(selectors["next_page"])
        for rule in fields.values():
            soupsieve.compile(rule if isinstance(rule, str) else rule["css"])
            while isinstance(rule, dict) and (rule := rule.get("fallback")):
                soupsieve.compile(rule if isinstance(rule, str) else rule["css"])
        if pattern := target.get("product_id_pattern"):
            import re

            if re.compile(pattern).groups < 1:
                raise ValueError("Product ID pattern needs a capture group.")
        for source in target.get("sources") or [{}]:
            source_url(target, source)
    except (AttributeError, KeyError, TypeError, ValueError, soupsieve.SelectorSyntaxError) as exc:
        parser.error(f"Invalid TARGET configuration: {exc}")
    if LOCAL_BROWSERS.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(LOCAL_BROWSERS))
    try:
        with ExitStack() as stack:
            collection = None if options.no_mongo else connect(stack)
            output = None
            if collection is not None:
                progress(f"Saving products to MongoDB: {collection.full_name}")
            else:
                sys.stdout.reconfigure(encoding="utf-8")
                output = lambda record: print(json.dumps(record, ensure_ascii=False))
            stats = StoreCrawler(target, options, stack, collection, output).run()
    except (StorageError, OSError) as exc:
        progress(str(exc))
        return 2
    except KeyboardInterrupt:
        return 130
    progress(json.dumps({"records": stats["records"], **stats}))
    return 0 if stats["records"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
