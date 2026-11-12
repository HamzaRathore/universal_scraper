"""Discover and extract product records from public storefronts."""

import argparse
from contextlib import ExitStack
from io import StringIO
import json
import os
from pathlib import Path
import re
import sys
from time import monotonic
from types import SimpleNamespace
from urllib.parse import urlsplit

from db import StorageError, connect
from scraper_utils import progress, tagged_url, utc_now, web_url, write_json
from scraping.browser import render_error_diagnostic, render_local, save_error_diagnostic
from scraping.crawler import StoreCrawler
from scraping.extraction import FIELDS

# TARGET = {
#     "url": "https://pk.khaadi.com/ready-to-wear/chapter-2/kurta-/",
#     "tags": {"Chapter 2 Kurtas": "", "Essentials Kurtas": "/ready-to-wear/essentials/kurta/"},
# }
# TARGET = {
#     "url": "https://www.breakout.com.pk/collections/men-jacket",
#     "tags": {"Jackets / Coats": "", "Sweaters": "/collections/men-sweaters"},
# }
# TARGET = {
#     "url": "https://www.alexandermcqueen.com/en-us/ca/women/shoes",
#     "tags": {"Sneakers": "/en-us/ca/women/shoes/sneakers", "Boots": "/en-us/ca/women/shoes/boots"},
# }
# TARGET = {
#     "url": "https://www.vinted.co.uk/catalog/1231-shoes",
#     "tags": {"Boots": "?search_text=boots", "Trainers": "?search_text=trainers"},
# }
# TARGET = {
#     "url": "https://pk.lamaretail.com/collections/woman-boots",
#     "tags": {"Boots": "", "Loafers": "/collections/woman-loafers"},
# }
TARGET = {
    "url": "https://www.airbnb.com/homes",
    "tags": {"Islamabad": "/islamabad-pakistan/stays", "Lahore": "/lahore-pakistan/stays"},
}
MAX_PAGES = 1

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_DEPS = PROJECT_DIR / ".deps"
LOCAL_BROWSERS = PROJECT_DIR / ".browsers"

if LOCAL_DEPS.is_dir():
    sys.path.insert(0, str(LOCAL_DEPS))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="Optional single-URL override for the configured target")
    parser.add_argument("-o", "--output", default=str(PROJECT_DIR / "products.jsonl"), help="Output file or - for stdout")
    parser.add_argument("--mongo", action=argparse.BooleanOptionalAction, default=None,
                        help="Save to configured MongoDB by default. Use --no-mongo for JSONL only")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="Maximum discovery pages, excluding item detail pages")
    parser.add_argument("--max-products", type=int)
    parser.add_argument("--max-details", type=int, help="Maximum item detail requests after discovery")
    parser.add_argument("--max-sitemaps", type=int, help="Use 0 to skip sitemap discovery")
    parser.add_argument("--delay", type=float, default=4, help="Minimum seconds between page requests (default: 4)")
    parser.add_argument("--timeout", type=float, default=20, help="HTTP connection and read timeout in seconds")
    parser.add_argument("--retries", type=int, default=1, help="Maximum retries for temporary request failures")
    parser.add_argument("--max-retry-wait", type=float, default=60, help="Stop instead of waiting beyond this retry budget (default: 60)")
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--ca-bundle", default=os.getenv("SCRAPER_CA_BUNDLE"), help="Additional trusted PEM certificates")
    parser.add_argument("--allow-host", dest="allow_hosts", action="append", default=[], help="Additional hostname authorized for this crawl")
    parser.add_argument("--robots-cache", type=Path, default=PROJECT_DIR / ".robots-cache")
    parser.add_argument("--no-robots-cache", dest="robots_cache", action="store_const", const=None)
    parser.add_argument("--no-public-api", dest="public_api", action="store_false")
    parser.add_argument("--report", type=Path, help="Write structured job and URL diagnostics to this JSON file")
    parser.add_argument("--browser-memory-mb", type=int, default=768)
    parser.add_argument("--browser-timeout", type=float, default=40)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--feed", help="Authorized Schema.org JSON feed file or in-scope URL")
    source.add_argument("--html-file", type=Path, help="Extract product data from a local HTML file without crawling")
    parser.add_argument("--browser", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--wait-selector", help="Optional CSS selector that indicates rendered content is ready")
    parser.add_argument("--render-wait", type=float, help="Seconds to wait for rendered content (default: 12 listing, 5 detail)")
    parser.add_argument("--selectors", help="Optional JSON file containing CSS rules for unstructured product pages")
    options = parser.parse_args()
    seed = web_url("", options.url or TARGET["url"], clean=True)
    if not seed:
        parser.error("Provide a full http:// or https:// URL.")
    tags = TARGET.get("tags", {}) if not (options.url or options.feed or options.html_file) else {}
    try:
        tagged_seeds = [(tag, tagged_url(seed, target)) for tag, target in tags.items()]
    except ValueError as exc:
        parser.error(str(exc))
    for name in options.allow_hosts:
        if not re.fullmatch(r"[a-zA-Z0-9.-]+(?::\d+)?", name):
            parser.error("--allow-host requires a hostname or hostname:port.")
    options.allow_hosts = [name.lower().removeprefix("www.") for name in options.allow_hosts]
    if options.max_sitemaps is None:
        options.max_sitemaps = 12 if urlsplit(seed).path == "/" and not urlsplit(seed).query else 0
    if (options.max_pages < 1
            or (options.max_products is not None and options.max_products < 1)
            or (options.max_details is not None and options.max_details < 0)
            or options.max_sitemaps < 0 or not 0 <= options.delay < float("inf")
            or not 0 < options.timeout < float("inf")
            or not 0 <= options.retries <= 5 or options.max_depth < 0
            or not 0 <= options.max_retry_wait < float("inf")
            or options.browser_memory_mb < 0 or not 0 < options.browser_timeout < float("inf")
            or (options.render_wait is not None and not 0 <= options.render_wait < float("inf"))):
        parser.error("Page and product limits and timeout must be positive. Detail limit, sitemap limit and delay must be nonnegative.")
    if LOCAL_BROWSERS.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(LOCAL_BROWSERS))
    started_at, started = utc_now(), monotonic()
    crawler = None
    try:
        import bs4
        import protego
        if options.browser != "never":
            import psutil
    except ImportError:
        parser.error("Install scraper dependencies with python -m pip install -r requirements.txt.")
    options.rules = None
    if options.selectors:
        import soupsieve

        try:
            with open(options.selectors, encoding="utf-8") as config:
                rules = json.load(config)
            fields = rules["fields"]
            if "title" not in fields or set(fields) - (set(FIELDS) - {"offers"} | {"url"}):
                raise ValueError("Provide a title selector and use supported product field names.")
            for selector in [rules["container"], *fields.values()]:
                soupsieve.compile(selector)
            options.rules = rules
        except (OSError, ValueError, KeyError, TypeError, AttributeError, soupsieve.SelectorSyntaxError) as exc:
            parser.error(f"Invalid selector config: {exc}")
    try:
        with ExitStack() as stack:
            collection = connect(stack, optional=options.mongo is None) if options.mongo is not False else None
            if collection is not None:
                progress(f"Saving products to MongoDB: {collection.full_name}")
            if options.output == "-":
                sys.stdout.reconfigure(encoding="utf-8")
                output = sys.stdout
            else:
                output = stack.enter_context(open(options.output, "w", encoding="utf-8"))
            crawler = StoreCrawler(seed, options, output, stack, collection)
            stats = crawler.run_tags(tagged_seeds) if tagged_seeds else crawler.run()
    except KeyboardInterrupt:
        if crawler and options.report:
            write_json(options.report, crawler.report(started_at, started, "cancelled"))
        return 130
    except StorageError as exc:
        print(exc, file=sys.stderr)
        if crawler and options.report:
            crawler.issue("STORAGE_ERROR", seed, "storage", exc)
            write_json(options.report, crawler.report(started_at, started, "failed"))
        return 2
    except OSError as exc:
        progress(f"Scraper setup or output error: {exc}")
        return 2
    save_error_diagnostic(crawler)
    if options.report:
        state = "partial" if stats["records"] and (crawler.errors or stats["details_without_data"]) else (
            "completed" if stats["records"] else "failed"
        )
        write_json(options.report, crawler.report(started_at, started, state))
    print(json.dumps({"records": stats["records"], **stats}), file=sys.stderr)
    if not stats["records"] and stats["robots_unavailable"]:
        progress("No records extracted because robots.txt could not be verified. See the HTTP or connection error above.")
    elif not stats["records"] and not (stats["robots_skipped"] or stats["http_errors"]):
        progress("No records extracted. Try a direct product or accommodation listing URL.")
    return 0 if stats["records"] else 1


def browser_main():
    sys.stdout.reconfigure(encoding="utf-8")
    request = json.load(sys.stdin.buffer)
    if "diagnostic" in request:
        print(json.dumps(render_error_diagnostic(request["diagnostic"]), ensure_ascii=False))
        return
    options = SimpleNamespace(**request["options"])
    if options.robots_cache:
        options.robots_cache = Path(options.robots_cache)
    with ExitStack() as stack:
        crawler = StoreCrawler(request["seed"], options, StringIO(), stack)
        records, links = render_local(crawler, request["url"], discovery=request["discovery"],
                                      installed_chrome=request.get("installed_chrome", False))
    print(json.dumps({"records": records, "links": links, "errors": crawler.errors,
                      "redirects": crawler.redirects, "robots": crawler.robot_status,
                      "screenshots": crawler.screenshots,
                      "rate_limited": crawler.stats["rate_limited"]}, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--internal-render"]:
        browser_main()
    else:
        raise SystemExit(main())
