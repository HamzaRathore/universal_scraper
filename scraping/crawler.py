"""Discover permitted URLs and coordinate scraping and storage."""

from collections import Counter
import gzip
from heapq import heapify, heappop, heappush
from http.client import IncompleteRead
from io import BytesIO
from itertools import count
import json
from pathlib import Path
from random import uniform
import ssl
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from xml.etree import ElementTree

from db import save_product
from scraper_utils import (
    AGENT, MAX_BODY, RobotsCache, ScopeError, error_category, host, policy_delay,
    priority, progress, retry_after, room_url, same_page, utc_now, web_url,
)
from .browser import render
from .extraction import (
    FIELDS, api_products, detect_platform, inspect_html, normalize_record,
    product_records, public_endpoint, shopify_currency,
)

LOCAL_SOURCES = Path(__file__).resolve().parents[1] / "sources"


class ScopedRedirect(HTTPRedirectHandler):
    def __init__(self, scope, can_fetch, trace):
        self.scope = scope
        self.can_fetch = can_fetch
        self.trace = trace

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = web_url(req.full_url, newurl)
        self.trace.append({"from": req.full_url, "to": target, "status": code})
        if (not target or host(target) not in self.scope
                or (urlsplit(req.full_url).scheme == "https" and urlsplit(target).scheme != "https")):
            raise ScopeError(f"Redirect outside crawl scope: {newurl}")
        if urlsplit(req.full_url).path != "/robots.txt" and not getattr(req, "robots_lookup", False):
            if not self.can_fetch(target):
                raise ValueError(f"Redirect target disallowed by robots.txt: {target}")
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected and (urlsplit(req.full_url).path == "/robots.txt" or getattr(req, "robots_lookup", False)):
            redirected.robots_lookup = True
        return redirected


class StoreCrawler:
    def __init__(self, seed, options, output, stack, collection=None):
        self.seed = seed
        self.options = options
        self.output = output
        self.collection = collection
        self.stack = stack
        self.scope_hosts = {host(seed), *getattr(options, "allow_hosts", [])}
        self.redirects = []
        self.errors = []
        self.screenshots = []
        self.sitemaps_checked = False
        self.tag = None
        self.tag_runs = []
        self.completed_details = set()
        self.robot_status = {}
        self.methods = Counter()
        self.urls = {}
        self.platform = "unknown"
        self.response_headers = {}
        self.cache = RobotsCache(getattr(options, "robots_cache", None))
        context = ssl.create_default_context()
        if ca_bundle := getattr(options, "ca_bundle", None):
            context.load_verify_locations(cafile=ca_bundle)
        self.opener = build_opener(HTTPSHandler(context=context),
                                   ScopedRedirect(self.scope_hosts, self.can_fetch, self.redirects))
        self.robots = {}
        self.last_request = {}
        self.adaptive_delay = {}
        self.queue = []
        self.queued = set()
        self.sequence = count()
        self.seen = set()
        self.stats = Counter()

    def issue(self, category, url, stage, message, **details):
        self.errors.append(dict(category=category, url=url, stage=stage, message=str(message), **details))

    def report(self, started_at, started, status):
        return {
            "url": self.seed, "status": status, "started_at": started_at, "completed_at": utc_now(),
            "duration_seconds": round(monotonic() - started, 3), "platform": self.platform,
            "robots": self.robot_status, "stats": dict(self.stats), "methods": dict(self.methods),
            "errors": self.errors, "redirects": self.redirects, "urls": self.urls,
            "screenshots": self.screenshots, "tag_runs": self.tag_runs,
            "failed_products": self.stats["details_without_data"],
            "skipped_urls": sum(state == "skipped" for state in self.urls.values()),
        }

    def pace(self, url, delay=0):
        domain = host(url)
        last = self.last_request.get(domain)
        interval = max(delay, self.options.delay, self.adaptive_delay.get(domain, 0))
        if last is not None and interval:
            remaining = interval + uniform(0, .5) - (monotonic() - last)
            if remaining > 0:
                sleep(remaining)
        self.last_request[domain] = monotonic()

    def request(self, url, delay=0):
        self.response_headers = {}
        retries = getattr(self.options, "retries", 1)
        budget = getattr(self.options, "max_retry_wait", 60)
        for attempt in range(retries + 1):
            server_delay = 0
            limited = False
            self.pace(url, delay)
            try:
                headers = {"User-Agent": AGENT, "Accept-Encoding": "gzip"}
                with self.opener.open(Request(url, headers=headers), timeout=self.options.timeout) as response:
                    self.response_headers = response.headers
                    body = response.read(MAX_BODY + 1)
                    if len(body) > MAX_BODY:
                        raise ValueError("Response exceeds size limit")
                    if response.headers.get("Content-Encoding", "").lower().strip() == "gzip":
                        try:
                            with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
                                body = compressed.read(MAX_BODY + 1)
                        except (EOFError, gzip.BadGzipFile) as exc:
                            raise ValueError("Invalid gzip response") from exc
                        if len(body) > MAX_BODY:
                            raise ValueError("Decompressed response exceeds size limit")
                    return response.geturl(), body, response.headers.get_content_charset() or "utf-8"
            except HTTPError as exc:
                server_delay = retry_after(exc.headers.get("Retry-After"))
                limited = exc.code == 429
                if limited:
                    domain = host(url)
                    current = max(self.options.delay, delay, self.adaptive_delay.get(domain, 0))
                    self.adaptive_delay[domain] = max(current, min(30, max(2, current * 2)))
                if (attempt == retries or exc.code not in {429, 500, 502, 503, 504}
                        or max(server_delay, 15 if limited else 0) > budget):
                    raise
            except (URLError, TimeoutError, IncompleteRead) as exc:
                if attempt == retries or isinstance(getattr(exc, "reason", exc), ssl.SSLError):
                    if isinstance(exc, IncompleteRead):
                        raise OSError("Incomplete HTTP response") from exc
                    raise
            backoff = (15 if limited else 1) * 2 ** attempt + uniform(0, .5)
            wait = max(server_delay, min(budget, backoff))
            self.stats["request_retries"] += 1
            if limited:
                progress(f"HTTP 429 from {host(url)}. Waiting {wait:.1f}s before retry "
                         f"with at least {self.adaptive_delay[host(url)]:g}s between later requests.")
            sleep(wait)

    def policy(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            from protego import Protego

            parser = None
            robots_url = f"{origin}/robots.txt"
            cached = self.cache.read(origin)
            if cached is not None:
                self.robot_status[origin] = "cached"
                self.robots[origin] = Protego.parse(cached)
                return self.robots[origin]
            progress(f"Checking robots.txt: {robots_url}")
            try:
                _, body, _ = self.request(robots_url)
                rules = body.decode("utf-8-sig", errors="replace")
                if (rules.lstrip().startswith("<")
                        or self.response_headers.get("Content-Type", "").lower().startswith("text/html")):
                    raise ValueError("received HTML instead of robots.txt")
                parser = Protego.parse(rules)
                self.robot_status[origin] = "fetched"
                self.cache.save(origin, rules, self.response_headers.get("Cache-Control", ""))
            except HTTPError as exc:
                if exc.code in {404, 410}:
                    parser = Protego.parse("")
                    self.robot_status[origin] = "missing"
                    self.cache.save(origin, "", exc.headers.get("Cache-Control", ""))
                else:
                    self.robot_status[origin] = f"http_{exc.code}"
                    self.issue(error_category(exc, "robots"), robots_url, "robots", exc, status=exc.code,
                               retry_after=retry_after(exc.headers.get("Retry-After")))
                    if exc.code == 429:
                        self.stats["rate_limited"] += 1
                    progress(f"Cannot read {robots_url}: {exc}")
            except (OSError, ValueError) as exc:
                category = error_category(exc, "robots")
                self.robot_status[origin] = category.lower()
                self.issue(category, robots_url, "robots", exc)
                progress(f"Cannot read {robots_url}: {exc}")
            self.robots[origin] = parser
        return self.robots[origin]

    def can_fetch(self, url):
        policy = self.policy(url)
        if policy is None:
            raise ValueError(f"robots.txt unavailable for {url}")
        return policy.can_fetch(url, AGENT)

    def enqueue(self, rank, url, depth=0):
        if host(url) in self.scope_hosts and url not in self.queued and depth <= getattr(self.options, "max_depth", 6):
            self.queued.add(url)
            self.urls.setdefault(url, "pending")
            heappush(self.queue, (rank, next(self.sequence), url, depth))

    def discover_sitemaps(self):
        if self.sitemaps_checked or not self.options.max_sitemaps:
            return
        self.sitemaps_checked = True
        parts = urlsplit(self.seed)
        origin = f"{parts.scheme}://{parts.netloc}"
        policy = self.policy(self.seed)
        if policy is None:
            return
        urls = [*policy.sitemaps, f"{origin}/sitemap.xml"]
        visited = set()
        while urls and len(visited) < self.options.max_sitemaps:
            url = web_url(origin, urls.pop(0))
            if not url or host(url) not in self.scope_hosts or url in visited:
                continue
            visited.add(url)
            sitemap_policy = self.policy(url)
            if sitemap_policy is None or not sitemap_policy.can_fetch(url, AGENT):
                continue
            try:
                progress(f"Reading sitemap {len(visited)}/{self.options.max_sitemaps}: {url}")
                _, body, _ = self.request(url, policy_delay(sitemap_policy))
                if body.startswith(b"\x1f\x8b"):
                    with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
                        body = compressed.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    continue
                root = ElementTree.fromstring(body)
            except (OSError, ValueError, EOFError, ElementTree.ParseError) as exc:
                self.issue(error_category(exc, "sitemap"), url, "sitemap", exc)
                if isinstance(exc, HTTPError) and exc.code == 429:
                    self.stats["rate_limited"] += 1
                    break
                continue
            kind = root.tag.rsplit("}", 1)[-1]
            for loc in root.findall("./*/{*}loc"):
                target = web_url(url, loc.text, clean=True)
                if not target or host(target) not in self.scope_hosts:
                    continue
                if kind == "sitemapindex":
                    urls.append(target)
                elif kind == "urlset" and (rank := priority(target)) is not None:
                    self.enqueue(rank, room_url(target) or target)
            urls.sort(key=lambda candidate: "product" not in candidate.lower())

    def scrape_page(self, url, *, detail=False):
        self.stats["attempted"] += 1
        self.urls[url] = "processing"
        stage = "detail" if detail else "listing"
        policy = self.policy(url)
        if policy is None:
            self.stats["robots_unavailable"] += 1
            self.urls[url] = "skipped"
            progress("Skipped because robots.txt could not be verified.")
            return url, [], []
        if not policy.can_fetch(url, AGENT):
            self.stats["robots_skipped"] += 1
            self.urls[url] = "skipped"
            self.issue("ROBOTS_DISALLOWED", url, stage, "URL disallowed by robots.txt")
            progress("Skipped by robots.txt.")
            return url, [], []
        try:
            final_url, body, encoding = self.request(url, policy_delay(policy))
            source = web_url(url, final_url, clean=True) or url
            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")
            platform = detect_platform(html, self.response_headers)
            if self.platform in {"unknown", "custom"}:
                self.platform = platform
            records, links = inspect_html(html, source, "http", self.options.rules)
            self.stats["fetched"] += 1
        except (OSError, ValueError) as exc:
            self.stats["http_errors"] += 1
            self.urls[url] = "failed"
            details = {"status": exc.code, "retry_after": retry_after(exc.headers.get("Retry-After"))} if isinstance(exc, HTTPError) else {}
            self.issue(error_category(exc, stage), url, stage, exc, **details)
            progress(f"HTTP error for {url}: {exc}")
            if isinstance(exc, HTTPError) and exc.code == 429:
                self.stats["rate_limited"] += 1
            return url, [], []

        detail_candidate = detail or priority(source) == 1
        if not records and getattr(self.options, "public_api", True):
            endpoint = public_endpoint(platform, source, detail_candidate)
            if endpoint:
                api_url = web_url(source, endpoint)
                try:
                    if self.can_fetch(api_url):
                        _, payload, _ = self.request(api_url, policy_delay(policy))
                        rows = api_products(platform, json.loads(payload),
                                            shopify_currency(html) if platform == "shopify" else None)
                        records.extend(product_records(rows, source, f"api:{platform}"))
                except (OSError, ValueError, TypeError, RecursionError) as exc:
                    self.issue(error_category(exc, "api"), api_url, "api", exc)
                    if isinstance(exc, HTTPError) and exc.code == 429:
                        self.stats["rate_limited"] += 1
        if detail_candidate:
            needs_browser = not any(
                not record["method"].endswith(":listing_card")
                and (same_page(record["url"], url) or same_page(record["url"], source))
                for record in records
            )
        else:
            # Category links help only when a discovery page is still available.
            more_pages = self.stats["discovery_pages"] < self.options.max_pages
            needs_browser = not records and not any(
                (rank == 1 or (more_pages and rank <= 2))
                and host(link) in self.scope_hosts for rank, link in links
            )
        if (self.options.browser == "always" or
                (self.options.browser == "auto" and needs_browser)) and not self.stats["rate_limited"]:
            self.pace(source, policy_delay(policy))
            self.stats["browser_attempted"] += 1
            progress(f"Trying browser rendering: {source}")
            rendered, rendered_links = render(self, source, discovery=not detail_candidate)
            records.extend(rendered)
            links.extend(rendered_links)
        self.urls[url] = "processed"
        return source, records, links

    def save_record(self, record):
        identity = (record["url"], record["sku"] or record["title"])
        if identity in self.seen:
            return
        self.seen.add(identity)
        if self.tag is not None:
            record.update(crawl_tag=self.tag, crawl_url=self.seed)
        normalize_record(record, self.platform)
        record["missing_fields"] = [field for field in FIELDS if record[field] is None or record[field] == []]
        if self.collection is not None:
            save_product(self.collection, record)
        self.output.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.output.flush()
        self.stats["records"] += 1
        self.methods[record["method"]] += 1
        self.stats["accommodations" if record["record_type"] == "accommodation" else "products"] += 1

    def product_limit_reached(self):
        limit = self.options.max_products
        return limit is not None and self.stats["records"] >= limit

    def save_imported_products(self, records, status):
        for record in records:
            if record["record_type"] != "product" or host(record["url"]) not in self.scope_hosts:
                continue
            if self.product_limit_reached():
                break
            record["detail_status"] = status
            self.urls[record["url"]] = "processed"
            self.save_record(record)

    def load_feed(self, feed):
        if not feed:
            return
        try:
            if web_url("", feed):
                if host(feed) not in self.scope_hosts or not self.can_fetch(feed):
                    raise ScopeError("Feed URL is outside scope or disallowed")
                _, body, _ = self.request(feed)
            else:
                with Path(feed).open("rb") as file:
                    body = file.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("Feed exceeds size limit")
            records = product_records(json.loads(body), self.seed, "feed:structured_json")
            self.save_imported_products(records, "fetched")
        except (OSError, ValueError, RecursionError) as exc:
            self.issue(error_category(exc, "feed"), self.seed, "feed", exc)
        if self.stats["records"]:
            self.stats["discovered_item_urls"] = len(self.urls)

    def load_html(self, path, *, require_canonical=False):
        progress(f"Reading local product HTML: {path}")
        try:
            with path.open("rb") as file:
                body = file.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ValueError("HTML file exceeds size limit")
            html = body.decode("utf-8-sig", errors="replace")
            source = self.seed
            if require_canonical:
                from bs4 import BeautifulSoup

                canonical = BeautifulSoup(html, "html.parser").find("link", rel="canonical", href=True)
                source = web_url(self.seed, canonical["href"], clean=True) if canonical else None
                if not source or host(source) not in self.scope_hosts:
                    raise ValueError("Saved product HTML needs an in-scope canonical URL")
            self.platform = detect_platform(html, {})
            records, _ = inspect_html(html, source, "local_html", self.options.rules)
        except (OSError, ValueError, RecursionError) as exc:
            self.issue("LOCAL_HTML_ERROR", self.seed, "local_html", exc)
            progress(f"Cannot import local HTML: {exc}")
            return
        self.save_imported_products(records, "imported")
        self.stats["discovered_item_urls"] = len(self.urls)
        if not self.stats["records"]:
            message = "No extractable product data in the saved HTML. Use a saved product detail page."
            self.issue("EXTRACTION_FAILED", self.seed, "local_html", message)
            progress(message)

    def load_local_fallback(self):
        hostname = urlsplit(self.seed).hostname.removeprefix("www.")
        if ":" in hostname:
            return
        feed = LOCAL_SOURCES / f"{hostname}.json"
        html = LOCAL_SOURCES / f"{hostname}.html"
        has_feed, has_html = feed.is_file(), html.is_file()
        if has_feed:
            progress(f"Live crawl produced no records. Importing local catalog: {feed}")
            self.load_feed(str(feed))
        if not self.stats["records"] and has_html:
            self.load_html(html, require_canonical=True)
        if not (has_feed or has_html):
            progress(f"No local fallback configured. Expected {feed.name} or {html.name} under {LOCAL_SOURCES}.")

    def discover(self):
        self.enqueue(0, self.seed)
        candidates = {}
        processed = set()
        ready = []
        while self.queue and self.stats["discovery_pages"] < self.options.max_pages:
            rank, _, url, depth = heappop(self.queue)
            if rank == 3 and candidates:
                continue
            self.stats["discovery_pages"] += 1
            progress(f"Discovery page {self.stats['discovery_pages']}/{self.options.max_pages}: {url}")
            source, records, links = self.scrape_page(url)
            processed.add(room_url(source) or source)
            for record in records:
                target = record["url"]
                if host(target) not in self.scope_hosts:
                    continue
                if same_page(target, source) and not record["method"].endswith(":listing_card"):
                    processed.add(target)
                    ready.append(record)
                elif depth < getattr(self.options, "max_depth", 6) and candidates.get(target) is None:
                    candidates[target] = record
                    self.urls.setdefault(target, "pending")
            for link_rank, link in links:
                if host(link) not in self.scope_hosts or link in processed:
                    continue
                if link_rank == 1 and depth < getattr(self.options, "max_depth", 6):
                    candidates.setdefault(link, None)
                    self.urls.setdefault(link, "pending")
                elif link_rank != 1:
                    self.enqueue(link_rank, link, depth + 1)
            if self.stats["rate_limited"]:
                break
            if (self.stats["discovery_pages"] == 1 and not candidates and not ready
                    and getattr(self.options, "max_depth", 6) > 0
                    and not any(rank <= 2 for rank, _, _, _ in self.queue)):
                self.discover_sitemaps()
                sitemap_details = (target for rank, _, target, _ in sorted(self.queue) if rank == 1)
                for target in sitemap_details:
                    candidates.setdefault(target, None)
                self.queue = [entry for entry in self.queue if entry[0] != 1]
                heapify(self.queue)

        targets = {url: card for url, card in candidates.items() if url not in processed}
        self.stats["discovered_item_urls"] = len(targets) + len({row["url"] for row in ready})
        progress(f"Discovery complete: {self.stats['discovered_item_urls']} unique item URLs.")
        if self.stats["discovery_pages"] >= self.options.max_pages and any(
            rank <= 2 for rank, _, _, _ in self.queue
        ):
            progress("Discovery limit reached with more collection or pagination links queued.")
        return targets, ready

    def extract_details(self, targets, ready):
        for record in ready:
            if self.product_limit_reached():
                break
            record["detail_status"] = "fetched"
            self.save_record(record)
        for index, (url, card) in enumerate(targets.items(), 1):
            if self.stats["rate_limited"] or self.product_limit_reached():
                break
            if self.tag is not None and url in self.completed_details:
                self.urls[url] = "processed"
                continue
            if self.options.max_details is not None and self.stats["detail_pages"] >= self.options.max_details:
                progress("Detail page limit reached.")
                break
            progress(f"Detail {index}/{len(targets)}: {url}")
            self.stats["detail_pages"] += 1
            source, records, _ = self.scrape_page(url, detail=True)
            details = [record for record in records
                       if (same_page(record["url"], url) or same_page(record["url"], source))
                       and not record["method"].endswith(":listing_card")]
            if not details:
                self.stats["details_without_data"] += 1
                if self.urls.get(url) == "processed":
                    self.urls[url] = "failed"
                    self.issue("EXTRACTION_FAILED", url, "detail", "No matching product data")
                if card:
                    card["detail_status"] = "card_only"
                    self.save_record(card)
                continue
            for record in details:
                if self.product_limit_reached():
                    break
                record["detail_status"] = "fetched"
                if card:
                    for field in FIELDS:
                        if record[field] in (None, []):
                            record[field] = card[field]
                    if card.get("price_display"):
                        record["price_display"] = card["price_display"]
                    record["listing_source_url"] = card["source_url"]
                self.save_record(record)
            if self.tag is not None:
                self.completed_details.update((url, source))

    def run_tags(self, tagged_seeds):
        totals = Counter()
        urls = {}
        original_seed = self.seed
        try:
            for tag, seed in tagged_seeds:
                self.tag, self.seed = tag, seed
                self.stats = Counter()
                self.urls = {}
                self.queue.clear()
                self.queued.clear()
                self.sitemaps_checked = False
                progress(f"Scraping tag: {tag} -> {seed}")
                try:
                    self.run()
                finally:
                    totals.update(self.stats)
                    urls.update(self.urls)
                    self.tag_runs.append({"tag": tag, "url": seed, "stats": dict(self.stats)})
                if self.stats["rate_limited"]:
                    progress(f"Rate limit reached during tag {tag}. Remaining tags will wait for the next run.")
                    break
        finally:
            self.tag, self.seed = None, original_seed
            self.stats, self.urls = totals, urls
        return totals

    def run(self):
        if html_file := getattr(self.options, "html_file", None):
            self.load_html(html_file)
            return self.stats
        progress(f"Starting discovery: {self.seed} (up to {self.options.max_pages} discovery pages)")
        feed = getattr(self.options, "feed", None)
        self.load_feed(feed)
        if self.stats["records"]:
            return self.stats
        targets, ready = self.discover()
        self.extract_details(targets, ready)
        if (not self.stats["records"] and not self.stats["rate_limited"]
                and not self.stats["robots_unavailable"] and not self.sitemaps_checked
                and self.options.max_sitemaps and getattr(self.options, "max_depth", 6) > 0
                and (self.options.max_details is None or self.stats["detail_pages"] < self.options.max_details)):
            self.discover_sitemaps()
            alternate_targets = {
                url: None for rank, _, url, _ in sorted(self.queue)
                if rank == 1 and self.urls.get(url) == "pending" and url not in targets and url != self.seed
            }
            if alternate_targets:
                self.stats["discovered_item_urls"] += len(alternate_targets)
                progress(f"Sitemap fallback found {len(alternate_targets)} additional product URLs.")
                self.extract_details(alternate_targets, [])
        if not feed and not self.stats["records"]:
            self.load_local_fallback()
        if not self.stats["discovered_item_urls"] and not self.errors:
            self.issue("PRODUCT_DISCOVERY_FAILED", self.seed, "discovery", "No product URLs recognized")
        return self.stats
