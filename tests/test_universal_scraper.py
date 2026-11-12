from contextlib import ExitStack
import gzip
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.response import addinfourl
from urllib.error import HTTPError
from email.message import Message

from universal_scraper import StoreCrawler
from scraper_utils import RobotsCache, price_number, priority, retry_after, tagged_url
from scraping.browser import render, wait_for_product_content
from scraping.extraction import api_products, detect_platform, inspect_html, product_records
from db import connect, save_product


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.seed = "https://shop.example/"
        self.output = StringIO()
        self.stack = self.enterContext(ExitStack())
        options = SimpleNamespace(delay=0, timeout=1, max_pages=1, max_details=2,
                                  max_products=2, max_sitemaps=1, browser="never", rules=None)
        self.crawler = StoreCrawler(self.seed, options, self.output, self.stack)

    def test_robots_wildcards_and_longest_rule(self):
        rules = "User-agent: *\nAllow: /\nDisallow: /products/*?*private=\nAllow: /products/public?private=1$"
        with patch.object(self.crawler, "request", return_value=(self.seed, rules.encode(), "utf-8")):
            self.assertFalse(self.crawler.can_fetch(self.seed + "products/other?private=1"))
            self.assertTrue(self.crawler.can_fetch(self.seed + "products/public?private=1"))
            self.assertFalse(self.crawler.can_fetch(self.seed + "products/public?private=10"))

    def test_unreachable_robots_is_reported_separately(self):
        with patch.object(self.crawler, "request", side_effect=TimeoutError("test timeout")) as request:
            stats = self.crawler.run()
        self.assertEqual(stats["robots_unavailable"], 1)
        self.assertEqual(stats["robots_skipped"], 0)
        request.assert_called_once_with(self.seed + "robots.txt")

    def test_gzip_response_is_decoded(self):
        headers = Message()
        headers["Content-Encoding"] = "gzip"
        response = addinfourl(BytesIO(gzip.compress(b"hello")), headers, self.seed, 200)
        with patch.object(self.crawler.opener, "open", return_value=response):
            self.assertEqual(self.crawler.request(self.seed), (self.seed, b"hello", "utf-8"))

    def test_sitemap_detail_budget_is_independent_of_discovery_budget(self):
        urls = [self.seed + "products/one", self.seed + "products/two"]
        product_pages = {
            url: '<script type="application/ld+json">' + json.dumps({
                "@type": "Product", "name": url.rsplit("/", 1)[-1], "url": url,
            }) + "</script>" for url in urls
        }
        pages = {
            **product_pages,
            self.seed: "<html><title>Store</title></html>",
            self.seed + "robots.txt": "User-agent: *\nAllow: /",
            self.seed + "sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                       + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>",
        }
        with patch.object(self.crawler, "request", side_effect=lambda url, *_: (url, pages[url].encode(), "utf-8")):
            stats = self.crawler.run()
        self.assertEqual(stats["discovery_pages"], 1)
        self.assertEqual(stats["detail_pages"], 2)
        self.assertEqual(stats["records"], 2)

    def test_sitemap_fallback_after_listing_product_yields_no_data(self):
        failed = self.seed + "products/empty"
        recovered = self.seed + "products/jacket"
        pages = {
            self.seed + "robots.txt": "User-agent: *\nAllow: /",
            self.seed: f'<a href="{failed}">Empty product</a>',
            failed: "<html><title>Empty product</title></html>",
            self.seed + "sitemap.xml": f"<urlset><url><loc>{recovered}</loc></url></urlset>",
            recovered: '<script type="application/ld+json">{"@type":"Product","name":"Jacket"}</script>',
        }
        with patch.object(self.crawler, "request", side_effect=lambda url, *_: (url, pages[url].encode(), "utf-8")):
            stats = self.crawler.run()
        self.assertEqual(stats["detail_pages"], 2)
        self.assertEqual(stats["records"], 1)
        self.assertEqual(json.loads(self.output.getvalue())["url"], recovered)

    def test_tagged_url_keeps_existing_filters_and_replaces_search_term(self):
        base = self.seed + "catalog/shoes?size=42&search_text=old"
        self.assertEqual(tagged_url(base, "?search_text=ankle boots"),
                         self.seed + "catalog/shoes?size=42&search_text=ankle+boots")
        self.assertEqual(tagged_url(base, "/catalog/boots"), self.seed + "catalog/boots")
        with self.assertRaises(ValueError):
            tagged_url(base, "https://other.example/catalog/boots")

    def test_tags_share_robots_and_deduplicate_products(self):
        first, second = self.seed + "collections/first", self.seed + "collections/second"
        shared, unique = self.seed + "products/shared", self.seed + "products/unique"
        pages = {
            self.seed + "robots.txt": "User-agent: *\nAllow: /",
            first: f'<a href="{shared}">Shared</a>',
            second: f'<a href="{shared}">Shared</a><a href="{unique}">Unique</a>',
            shared: '<script type="application/ld+json">{"@type":"Product","name":"Shared"}</script>',
            unique: '<script type="application/ld+json">{"@type":"Product","name":"Unique"}</script>',
        }
        with patch.object(self.crawler, "request", side_effect=lambda url, *_: (url, pages[url].encode(), "utf-8")) as request:
            stats = self.crawler.run_tags([("First", first), ("Second", second)])
        self.assertEqual(stats["records"], 2)
        self.assertEqual([json.loads(line)["crawl_tag"] for line in self.output.getvalue().splitlines()],
                         ["First", "Second"])
        self.assertEqual([call.args[0] for call in request.call_args_list].count(self.seed + "robots.txt"), 1)
        self.assertEqual([call.args[0] for call in request.call_args_list].count(shared), 1)
        self.assertEqual([row["stats"]["discovery_pages"] for row in self.crawler.tag_runs], [1, 1])

    def test_product_variants_inherit_group_fields_and_keep_distinct_prices(self):
        product_url = self.seed + "products/coat"
        document = {
            "@type": "ProductGroup", "name": "Coat", "productGroupID": "coat",
            "brand": {"name": "Example"}, "description": "Wool coat",
            "hasVariant": [
                {"@type": "Product", "sku": "S", "size": "small", "offers": {
                    "url": product_url + "?size=s", "price": 99, "priceCurrency": "USD",
                }},
                {"@type": "Product", "sku": "L", "size": "large", "offers": {
                    "url": product_url + "?size=l",
                    "priceSpecification": {"price": 109, "priceCurrency": "USD"},
                }},
            ],
        }
        rows = list(product_records(document, product_url, "http:structured_json"))
        self.assertEqual([(row["sku"], row["price"]) for row in rows], [("S", 99), ("L", 109)])
        self.assertTrue(all(row["brand"] == "Example" and row["product_group_id"] == "coat" for row in rows))
        self.assertEqual({row["url"] for row in rows}, {product_url + "?size=s", product_url + "?size=l"})

    def test_robots_cache_honors_no_store(self):
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            cache = RobotsCache(Path(directory))
            cache.save(self.seed, "User-agent: *\nDisallow: /", "max-age=60")
            self.assertIn("Disallow", cache.read(self.seed))
            cache.save(self.seed, "", "no-store")
            self.assertIsNone(cache.read(self.seed))

    def test_rate_limit_waits_and_retries(self):
        headers = Message()
        headers["Retry-After"] = "25"
        self.crawler.options.delay = 4
        limited = HTTPError(self.seed, 429, "Too Many Requests", headers, None)
        response = addinfourl(BytesIO(b"ok"), Message(), self.seed, 200)
        with patch.object(self.crawler.opener, "open", side_effect=[limited, response]), patch("scraping.crawler.sleep") as sleep:
            self.assertEqual(self.crawler.request(self.seed)[1], b"ok")
            sleep.assert_any_call(25)
        self.assertEqual(self.crawler.stats["request_retries"], 1)
        self.assertEqual(self.crawler.adaptive_delay["shop.example"], 8)
        self.assertGreater(retry_after("Wed, 21 Oct 2099 07:28:00 GMT"), 1)

    def test_rate_limit_without_header_still_waits_before_retry(self):
        limited = HTTPError(self.seed, 429, "Too Many Requests", Message(), None)
        response = addinfourl(BytesIO(b"ok"), Message(), self.seed, 200)
        with (patch.object(self.crawler.opener, "open", side_effect=[limited, response]),
              patch("scraping.crawler.uniform", return_value=0),
              patch("scraping.crawler.sleep") as sleep):
            self.assertEqual(self.crawler.request(self.seed)[1], b"ok")
            sleep.assert_any_call(15)

    def test_rate_limit_over_wait_budget_does_not_retry(self):
        headers = Message()
        headers["Retry-After"] = "120"
        limited = HTTPError(self.seed, 429, "Too Many Requests", headers, None)
        with patch.object(self.crawler.opener, "open", side_effect=limited) as request:
            with self.assertRaises(HTTPError):
                self.crawler.request(self.seed)
        request.assert_called_once()

    def test_pacing_uses_the_slowest_interval_for_each_host(self):
        self.crawler.options.delay = 4
        self.crawler.adaptive_delay["shop.example"] = 8
        self.crawler.last_request["shop.example"] = 100
        with (patch("scraping.crawler.monotonic", side_effect=[103, 109.25, 109.25]),
              patch("scraping.crawler.uniform", return_value=.25),
              patch("scraping.crawler.sleep") as sleep):
            self.crawler.pace(self.seed, delay=9)
            self.crawler.pace("https://other.example/")
        sleep.assert_called_once_with(6.25)

    def test_access_denied_policy_is_not_permission(self):
        denied = HTTPError(self.seed, 403, "Forbidden", Message(), None)
        with patch.object(self.crawler.opener, "open", side_effect=denied) as request:
            self.assertIsNone(self.crawler.policy(self.seed))
        self.assertEqual(self.crawler.errors[0]["category"], "HTTP_403")
        self.assertEqual(request.call_count, 1)

    def test_blocked_robots_uses_local_source_without_requesting_storefront(self):
        denied = HTTPError(self.seed, 403, "Forbidden", Message(), None)
        url = self.seed + "products/jacket"
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            sources = Path(directory)
            (sources / "shop.example.html").write_text(
                f'<link rel="canonical" href="{url}"><script type="application/ld+json">'
                '{"@type":"Product","name":"Fixture jacket"}</script>', encoding="utf-8",
            )
            with (patch("scraping.crawler.LOCAL_SOURCES", sources),
                  patch.object(self.crawler, "request", side_effect=denied) as request):
                stats = self.crawler.run()
        request.assert_called_once_with(self.seed + "robots.txt")
        self.assertEqual(stats["records"], 1)
        record = json.loads(self.output.getvalue())
        self.assertEqual(record["url"], url)
        self.assertEqual(record["method"], "local_html:structured_json")

    def test_platform_and_price_adapters(self):
        self.assertEqual(detect_platform("<script src='/plugins/woocommerce/main.js'>", {}), "woocommerce")
        self.assertEqual(price_number("1,234.50"), 1234.5)
        self.assertEqual(price_number("1.234,50"), 1234.5)
        self.assertIsNone(price_number("1.234.50"))
        rows = api_products("woocommerce", [{"name": "Test", "permalink": self.seed + "products/a",
                             "prices": {"price": "120", "currency_minor_unit": 0, "currency_code": "JPY"}}])
        self.assertEqual(rows[0]["offers"]["price"], "120")

    def test_gap_canonical_parent_retains_variant_url(self):
        source = "https://www.gap.com/browse/product.do?pid=1180239022&cl=true"
        canonical = "https://www.gap.com/browse/product.do?pid=1180239022"
        variant = canonical + "0001"
        html = f'<link rel="canonical" href="{canonical}"><script type="application/ld+json">' + json.dumps({
            "@type": "Product", "name": "Perfume", "sku": "1180239022", "offers": {"url": variant},
        }) + "</script>"
        records, _ = inspect_html(html, source, "http")
        self.assertEqual(records[0]["url"], canonical)
        self.assertEqual(records[0]["variant_url"], variant)

    def test_product_tiles_discover_nonstandard_detail_paths(self):
        source = "https://pk.khaadi.com/ready-to-wear/chapter-2/kurta-/"
        html = """
        <a href="/help/guide.html">Guide</a>
        <div class="product-tile" data-pid="SKU-123">
          <a href="/floral-kurta/SKU-123.html?source=plp&utm_source=listing&amp;size=M">Floral Kurta</a>
          <a href="/account/login.html">Sign in</a>
        </div>
        """
        _, links = inspect_html(html, source, "http")
        self.assertEqual([url for rank, url in links if rank == 1],
                         ["https://pk.khaadi.com/floral-kurta/SKU-123.html?size=M"])

    def test_pr_product_links_are_recognized_on_any_host(self):
        html = '<a href="/en-us/pr/ryder-boot-A00BFTWIAL82116.html">Ryder Boot</a>'
        for origin in ("https://www.alexandermcqueen.com", "https://shop.example"):
            with self.subTest(origin=origin):
                _, links = inspect_html(html, origin + "/en-us/ca/women/shoes", "http")
                self.assertEqual(links, [(1, origin + "/en-us/pr/ryder-boot-A00BFTWIAL82116.html")])
        self.assertNotEqual(priority("https://shop.example/pr/press-release.html"), 1)

    def test_numeric_product_suffix_is_distinct_from_category_suffix(self):
        self.assertEqual(priority("https://www.zara.com/in/en/capris-p01528801.html"), 1)
        self.assertEqual(priority("https://shop.example/en/winter-jacket-p01528801.html"), 1)
        self.assertEqual(priority("https://us.burberry.com/short-coat-p81219051"), 1)
        self.assertEqual(priority("https://www.zara.com/ww/en/woman-jackets-l1114.html?v1=2417772"), 2)

    def test_generic_product_price_markup_requires_product_path(self):
        html = '<h1>Jacket</h1><div class="prices"><span content="6990.00">Rs. 6,990</span></div>'
        records, _ = inspect_html(html, self.seed + "products/jacket", "http")
        self.assertEqual(records[0]["price"], "6990.00")
        self.assertEqual(inspect_html(html, self.seed + "collections/jackets", "http")[0], [])

    def test_collection_prefers_grid_products_over_navigation_links(self):
        html = """
        <nav><a href="/products/gift-card">Gift card</a></nav>
        <div class="product-item"><a href="/products/jacket">Jacket</a></div>
        """
        _, links = inspect_html(html, self.seed + "collections/jackets", "http")
        self.assertEqual(links, [(1, self.seed + "products/jacket")])

    def test_city_landing_extracts_rooms_instead_of_city_rating(self):
        url = "https://www.example.com/city/stays"
        html = '<script type="application/ld+json">' + json.dumps([
            {"@type": "Product", "name": "City rentals", "aggregateRating": {"ratingValue": 5}},
            {"@type": "ItemList", "itemListElement": [
                {"@type": "ListItem", "item": {
                    "@type": "VacationRental", "name": "Villa", "url": "www.example.com/rooms/123",
                }},
            ]},
        ]) + "</script>"
        records, _ = inspect_html(html, url, "http")
        self.assertEqual([(row["url"], row["record_type"]) for row in records],
                         [("https://www.example.com/rooms/123", "accommodation")])

    def test_listing_scrolls_once_only_after_readiness_timeout(self):
        self.crawler.options.render_wait = 12
        self.crawler.options.wait_selector = None
        page = Mock()
        wait = page.locator.return_value.first.wait_for
        wait.side_effect = [TimeoutError(), None]
        with patch("scraping.browser.monotonic", side_effect=[0, 2.5, 2.5]):
            wait_for_product_content(self.crawler, page, True, TimeoutError)
        page.evaluate.assert_called_once()
        self.assertEqual([call.kwargs["timeout"] for call in wait.call_args_list], [2500, 9500])

        page.reset_mock()
        wait.side_effect = None
        wait_for_product_content(self.crawler, page, True, TimeoutError)
        page.evaluate.assert_not_called()

    def test_chrome_fallback_after_empty_render(self):
        url = self.seed + "products/jacket"
        row = {"url": url, "method": "chrome:structured_json"}
        rules = (self.seed, b"User-agent: *\nAllow: /", "utf-8")
        with (patch.object(self.crawler, "request", return_value=rules),
              patch("scraping.browser.render_worker", side_effect=[([], []), ([row], [])]) as worker):
            self.assertEqual(render(self.crawler, url, discovery=False), ([row], []))
        self.assertEqual(worker.call_count, 2)
        self.assertTrue(worker.call_args.kwargs["installed_chrome"])

    def test_chrome_fallback_does_not_retry_access_denied(self):
        def blocked(*args, **kwargs):
            self.crawler.issue("HTTP_403", self.seed, "browser", "Forbidden", status=403)
            return [], []

        with patch("scraping.browser.render_worker", side_effect=blocked) as worker:
            self.assertEqual(render(self.crawler, self.seed, discovery=True), ([], []))
        worker.assert_called_once()

    def test_mongo_upsert_preserves_existing_identity(self):
        collection = Mock()
        record = {"url": self.seed + "products/test", "sku": "sku", "title": "Product", "price": 30}
        save_product(collection, record)
        save_product(collection, {**record, "price": 32})
        first_write, next_write = collection.update_one.call_args_list
        self.assertEqual(first_write.args[0], next_write.args[0])
        self.assertEqual(next_write.args[1]["$set"]["price"], 32)
        self.assertTrue(next_write.kwargs["upsert"])

    def test_mongo_uses_collection_from_dotenv_over_shell(self):
        with (patch.dict("os.environ", {"MONGODB_COLLECTION": "products"}),
              patch("dotenv.dotenv_values", return_value={
                  "MONGODB_URI": "mongodb://example.test", "MONGODB_DATABASE": "catalog",
                  "MONGODB_COLLECTION": "khadi",
              }), patch("pymongo.MongoClient") as mongo_client):
            collection = connect(self.stack)
        client = mongo_client.return_value.__enter__.return_value
        client.__getitem__.assert_called_once_with("catalog")
        client.__getitem__.return_value.__getitem__.assert_called_once_with("khadi")
        self.assertIs(collection, client.__getitem__.return_value.__getitem__.return_value)


if __name__ == "__main__":
    unittest.main()
