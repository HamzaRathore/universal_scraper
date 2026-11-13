"""Crawl configured listing URLs and save selector-based product records."""

from collections import Counter
import gzip
from http.client import IncompleteRead
from io import BytesIO
from random import uniform
import ssl
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from db import save_product
from scraper_utils import AGENT, MAX_BODY, RobotsCache, ScopeError, host, policy_delay, progress, retry_after, web_url
from .browser import render
from .extraction import listing_links, product_record, source_url, with_query


class ScopedRedirect(HTTPRedirectHandler):
    def __init__(self, domain, can_fetch):
        self.domain = domain
        self.can_fetch = can_fetch

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = web_url(req.full_url, newurl)
        if (not target or host(target) != self.domain
                or (urlsplit(req.full_url).scheme == "https" and urlsplit(target).scheme != "https")):
            raise ScopeError(f"Redirect outside crawl scope: {newurl}")
        robots = getattr(req, "robots_lookup", False) or urlsplit(req.full_url).path == "/robots.txt"
        if not robots and not self.can_fetch(target):
            raise ScopeError(f"Redirect disallowed by robots.txt: {target}")
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected and robots:
            redirected.robots_lookup = True
        return redirected


class StoreCrawler:
    def __init__(self, target, options, stack, collection, output=None):
        self.target = target
        self.options = options
        self.stack = stack
        self.collection = collection
        self.output = output
        self.domain = host(target["url"])
        self.robots = {}
        self.response_headers = {}
        self.cache = RobotsCache(options.robots_cache)
        self.last_request = {}
        self.adaptive_delay = {}
        self.seen = set()
        self.discovered_links = set()
        self.stats = Counter()
        self.browser = None
        context = ssl.create_default_context()
        if options.ca_bundle:
            context.load_verify_locations(cafile=options.ca_bundle)
        self.opener = build_opener(HTTPSHandler(context=context), ScopedRedirect(self.domain, self.can_fetch))

    def pace(self, url, delay=0):
        domain = host(url)
        last = self.last_request.get(domain)
        interval = max(delay, self.options.delay, self.adaptive_delay.get(domain, 0))
        if last is not None and interval:
            remaining = interval + uniform(0, .5) - (monotonic() - last)
            if remaining > 0:
                if remaining >= 1:
                    progress(f"Pacing: waiting {remaining:.1f}s before next request to {domain}")
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
                progress(f"Using cached robots policy for {origin}")
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
                self.cache.save(origin, rules, self.response_headers.get("Cache-Control", ""))
            except HTTPError as exc:
                if exc.code in {404, 410}:
                    parser = Protego.parse("")
                    self.cache.save(origin, "", exc.headers.get("Cache-Control", ""))
                else:
                    if exc.code == 429:
                        self.stats["rate_limited"] += 1
                    progress(f"Cannot read {robots_url}: {exc}")
            except (OSError, ValueError) as exc:
                progress(f"Cannot read {robots_url}: {exc}")
            self.robots[origin] = parser
        return self.robots[origin]

    def can_fetch(self, url):
        policy = self.policy(url)
        return policy is not None and policy.can_fetch(url, AGENT)

    def read_page(self, url):
        if not self.can_fetch(url):
            progress(f"Skipped by robots policy: {url}")
            self.stats["robots_skipped"] += 1
            return None, None
        try:
            final, body, encoding = self.request(url, policy_delay(self.policy(url)))
            return web_url(url, final, clean=True), body.decode(encoding, errors="replace")
        except (OSError, ValueError, LookupError) as exc:
            progress(f"Page fetch failed for {url}: {exc}")
            self.stats["failed_pages"] += 1
            if isinstance(exc, HTTPError) and exc.code == 429:
                self.stats["rate_limited"] += 1
            return None, None

    def scrape_source(self, source):
        url = source_url(self.target, source)
        selectors = self.target["selectors"]
        detail_params = self.target.get("detail_params", {}) | source.get("detail_params", {})
        saved = 0
        visited = set()
        for page_number in range(1, self.options.max_pages + 1):
            if not url or url in visited:
                break
            visited.add(url)
            progress(f"Listing {page_number}/{self.options.max_pages}: fetching {url}")
            final, html = self.read_page(url)
            if html is None:
                break
            links, next_page = listing_links(html, final, selectors)
            if not links and self.options.browser:
                html = render(self, final, selectors["product_links"])
                links, next_page = listing_links(html, final, selectors) if html else ([], None)
            self.stats["discovered"] += len(links)
            self.discovered_links.update(links)
            progress(f"Listing {page_number}: found {len(links)} unique product links")
            for index, link in enumerate(links, 1):
                if self.stats["rate_limited"] or saved == self.options.max_products:
                    return
                if link in self.seen:
                    progress(f"Product {index}/{len(links)}: already saved, skipping {link}")
                    continue
                detail_url = with_query(link, detail_params)
                progress(f"Product {index}/{len(links)}: fetching {detail_url}")
                final, html = self.read_page(detail_url)
                if html is None:
                    continue
                product_url = link if detail_params else final
                record = product_record(html, product_url, self.target, source)
                if not record and self.options.browser:
                    fields = selectors["fields"]
                    rule = fields.get("name") or next(iter(fields.values()))
                    wait = rule if isinstance(rule, str) else rule["css"]
                    html = render(self, final, wait)
                    record = product_record(html, product_url, self.target, source) if html else None
                if record:
                    if self.collection is not None:
                        save_product(self.collection, record)
                        progress(f"Saved to MongoDB: {record.get('name', record['product_url'])}")
                    if self.output is not None:
                        self.output(record)
                        progress(f"Previewed product: {record.get('name', record['product_url'])}")
                    self.seen.update((link, final, record["product_url"]))
                    self.stats["records"] += 1
                    saved += 1
                else:
                    progress(f"No usable product data matched the configured selectors: {link}")
            if self.stats["rate_limited"] or saved == self.options.max_products:
                break
            url = next_page

    def run(self):
        sources = self.target.get("sources") or [{}]
        for index, source in enumerate(sources, 1):
            label = " / ".join(filter(None, (source.get("q"), source.get("l_subcategory")))) or "default"
            progress(f"Source {index}/{len(sources)}: {label}")
            before = self.stats["records"]
            self.scrape_source(source)
            progress(f"Source {label} complete: {self.stats['records'] - before} records")
            if self.stats["rate_limited"]:
                progress("Stopping remaining sources because the site rate limit was reached.")
                break
        self.stats["unique_discovered"] = len(self.discovered_links)
        return self.stats
