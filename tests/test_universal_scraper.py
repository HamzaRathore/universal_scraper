from contextlib import ExitStack
from email.message import Message
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from universal_scraper import StoreCrawler
from db import connect, save_product
from scraping.extraction import listing_links, product_record, source_url
from types import SimpleNamespace


class ScraperTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.options = SimpleNamespace(
            delay=0, timeout=1, retries=0, max_retry_wait=60, max_pages=1,
            max_products=1, robots_cache=None, ca_bundle=None, browser=False,
        )
        self.target = {
            "url": "https://shop.example/catalog?size=42",
            "brand": "fixture",
            "query_param": "search_text",
            "product_id_pattern": r"/items/([0-9]+)",
            "sources": [{"q": "ankle boots", "gender": "men", "l_category": "shoes"},
                        {"q": "trainers", "l_subcategory": "trainers"}],
            "selectors": {
                "product_links": ".product",
                "drop_product_query": {"referrer"},
                "metadata_fields": ("query",),
                "fields": {"name": "h1", "price": ".price", "currency": ".currency",
                           "pictures": {"css": "img", "attr": "src"}},
            },
        }

    def test_source_query_preserves_other_filters(self):
        self.assertEqual(source_url(self.target, self.target["sources"][0]),
                         "https://shop.example/catalog?size=42&search_text=ankle+boots")
        with self.assertRaises(ValueError):
            source_url(self.target, {"path": "https://other.example/"})

    def test_listing_removes_only_configured_tracking_and_deduplicates(self):
        html = """<a class="product" href="/items/1?referrer=catalog&amp;variant=2">One</a>
                  <a class="product" href="/items/1?variant=2">One again</a>
                  <a class="product" href="https://other.example/items/2">External</a>"""
        self.assertEqual(listing_links(html, self.target["url"], self.target["selectors"])[0],
                         ["https://shop.example/items/1?variant=2"])

    def test_configured_json_ld_supports_listing_links_and_nested_fields(self):
        html = """
        <h1>Boots</h1>
        <script type="application/ld+json">{"@graph":[
            {"@type":"Product","url":"https://shop.example/items/1","offers":[{"price":49.5}]},
            {"@type":"Organization","url":"https://shop.example/"}
        ]}</script>
        """
        rule = {"css": 'script[type="application/ld+json"]', "types": {"Product"}}
        selectors = {**self.target["selectors"], "structured_links": rule}
        self.assertEqual(listing_links(html, self.target["url"], selectors)[0],
                         ["https://shop.example/items/1"])
        fields = {"name": "h1", "price": {**rule, "json_path": "offers.price"}}
        target = {**self.target, "selectors": {**selectors, "fields": fields}}
        row = product_record(html, "https://shop.example/items/1", target, {})
        self.assertEqual(row["price"], 49.5)

    def test_output_matches_product_schema(self):
        record = product_record('<h1>Boots</h1><span class="price">£12.50</span><img src="/boot.jpg">',
                                "https://shop.example/items/123-boots", self.target, self.target["sources"][0])
        self.assertEqual((record["p_id"], record["query"], record["name"], record["price"], record["currency"]),
                         ("123", "ankle boots", "Boots", 12.5, "GBP"))
        self.assertEqual(record["pictures"], ["https://shop.example/boot.jpg"])
        self.assertNotIn("refund", record)
        self.assertNotIn("sizes", record)

    def test_custom_selectors_save_only_matched_fields_and_url(self):
        target = {"brand": "fixture", "selectors": {"fields": {
            "custom_notes": ".notes", "missing": ".none",
            "gallery": {"css": "img", "attr": "src", "many": True, "url": True},
        }}}
        url = "https://shop.example/items/123"
        row = product_record('<p class="notes">Size 9</p><img src="/one.jpg"><img src="/two.jpg">',
                             url, target, {"q": "boots"})
        self.assertEqual(row, {"custom_notes": "Size 9", "product_url": url,
                               "gallery": ["https://shop.example/one.jpg", "https://shop.example/two.jpg"]})

    def test_vinted_gallery_filters_thumbnails_and_derives_brand_identity(self):
        url = "https://www.vinted.co.uk/items/123-boots"
        target = {"selectors": {
            "derive_brand_identity": True,
            "fields": {
                "name": "h1",
                "pictures": {
                    "css": 'img[data-testid^="item-photo-"], .item-photos img',
                    "attr": "src", "contains": "/t/", "exclude": "310x430",
                    "fallback": {"css": 'meta[property="og:image"]', "attr": "content"},
                },
            },
        }}
        html = """
        <h1>Boots</h1>
        <meta property="og:image" content="https://images.example/t/f800/og.webp">
        <img class="web_ui__Image__content" data-testid="item-photo-1--img" src="https://images.example/t/f800/one.webp?s=token">
        <img class="web_ui__Image__content" data-testid="item-photo-1--img" src="https://images.example/t/f800/one.webp?s=token">
        <img class="web_ui__Image__content" data-testid="item-photo-2--img" src="https://images.example/t/310x430/thumb.webp">
        <div class="item-photos"><img class="web_ui__Image__content" src="https://images.example/t/f800/two.webp"></div>
        """
        row = product_record(html, url, target, {})
        self.assertEqual(row["pictures"], ["https://images.example/t/f800/one.webp?s=token",
                                            "https://images.example/t/f800/two.webp"])
        self.assertEqual(row["brand_url"], "www.vinted.co.uk")
        self.assertEqual(row["logo"], "https://www.google.com/s2/favicons?sz=256&domain_url=https://www.vinted.co.uk")
        fallback = product_record('<h1>Boots</h1><meta property="og:image" content="https://images.example/t/f800/og.webp">',
                                  url, target, {})
        self.assertEqual(fallback["pictures"], ["https://images.example/t/f800/og.webp"])

    def test_tags_run_sequentially_with_one_policy_and_deduplicate(self):
        rows = []
        crawler = StoreCrawler(self.target, self.options, self.stack, None, rows.append)
        first, second = [source_url(self.target, source) for source in self.target["sources"]]
        product_one, product_two = "https://shop.example/items/1", "https://shop.example/items/2"
        robots = "https://shop.example/robots.txt"
        pages = {
            robots: "User-agent: *",
            first: f'<a class="product" href="{product_one}">Boots</a>',
            second: f'<a class="product" href="{product_one}">Duplicate</a><a class="product" href="{product_two}">Trainers</a>',
            product_one: "<h1>Boots</h1>", product_two: "<h1>Trainers</h1>",
        }
        with patch.object(crawler, "request", side_effect=lambda url, *_: (url, pages[url].encode(), "utf-8")) as request:
            self.assertEqual(crawler.run()["records"], 2)
        self.assertEqual([r["query"] for r in rows], ["ankle boots", "trainers"])
        self.assertEqual([call.args[0] for call in request.call_args_list], [robots, first, product_one, second, product_two])

    def test_robots_denial_stops_before_listing(self):
        crawler = StoreCrawler(self.target, self.options, self.stack, None)
        denied = HTTPError("https://shop.example/robots.txt", 403, "Forbidden", Message(), None)
        with patch.object(crawler, "request", side_effect=denied) as request:
            self.assertEqual(crawler.run()["records"], 0)
        request.assert_called_once_with("https://shop.example/robots.txt")

    def test_upsert_uses_product_identity_and_preserves_created_timestamp(self):
        collection = Mock()
        record = {"product_url": "https://shop.example/items/1", "p_id": "1", "name": "Boot", "price": 4}
        save_product(collection, record)
        save_product(collection, {**record, "price": 5})
        first, second = collection.update_one.call_args_list
        self.assertEqual(first.args[0], second.args[0])
        self.assertEqual(second.args[1]["$set"]["price"], 5)
        self.assertIn("created_at", second.args[1]["$setOnInsert"])
        self.assertTrue(second.kwargs["upsert"])

    def test_dotenv_selects_the_configured_collection(self):
        with (patch.dict("os.environ", {"MONGODB_COLLECTION": "old"}),
              patch("dotenv.dotenv_values", return_value={
                  "MONGODB_URI": "mongodb://example.test", "MONGODB_DATABASE": "catalog", "MONGODB_COLLECTION": "vinted",
              }), patch("pymongo.MongoClient") as mongo):
            connect(self.stack)
        client = mongo.return_value.__enter__.return_value
        client.__getitem__.assert_called_once_with("catalog")
        client.__getitem__.return_value.__getitem__.assert_called_once_with("vinted")


if __name__ == "__main__":
    unittest.main()
