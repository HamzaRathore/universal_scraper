"""Headless fallback for pages that need JavaScript to populate selectors."""

from contextlib import suppress
from urllib.parse import urlsplit

from scraper_utils import AGENT, policy_delay, progress


def render(crawler, url, wait_selector):
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    policy = crawler.policy(url)
    if policy is None or not policy.can_fetch(url, AGENT):
        return None
    progress(f"Trying headless Chromium for missing selector content: {url}")
    crawler.pace(url, policy_delay(policy))
    crawler.stats["browser_attempts"] += 1
    try:
        if crawler.browser is None:
            playwright = crawler.stack.enter_context(sync_playwright())
            crawler.browser = playwright.chromium.launch(headless=True, timeout=15000)
            crawler.stack.callback(crawler.browser.close)
        page = crawler.browser.new_page(user_agent=AGENT, service_workers="block")
        origin = urlsplit(url)[:2]

        def route_request(route):
            request = route.request
            if (crawler.stats["rate_limited"] or request.method not in {"GET", "HEAD"}
                    or urlsplit(request.url)[:2] != origin or not policy.can_fetch(request.url, AGENT)
                    or request.resource_type in {"image", "media", "font"}):
                route.abort()
                return
            try:
                response = route.fetch(max_redirects=0, timeout=12000)
                try:
                    if response.status == 429:
                        crawler.stats["rate_limited"] += 1
                    if 300 <= response.status < 400 and response.headers.get("location"):
                        route.abort()
                    else:
                        route.fulfill(response=response)
                finally:
                    response.dispose()
            except Error:
                with suppress(Error):
                    route.abort()

        try:
            page.route("**/*", route_request)
            response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if not response or not response.ok:
                if response:
                    progress(f"Browser HTTP {response.status} for {url}")
                return None
            ready = page.locator(wait_selector).first
            try:
                ready.wait_for(state="attached", timeout=3000)
            except TimeoutError:
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                with suppress(TimeoutError):
                    ready.wait_for(state="attached", timeout=7000)
            return page.content() if page.url == url else None
        finally:
            with suppress(Error):
                page.close()
    except Error as exc:
        progress(f"Browser failed for {url}: {str(exc).splitlines()[0]}")
        return None
