"""Isolate Chromium and wait for rendered product content."""

from contextlib import suppress
from html import escape
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
from time import monotonic, sleep
from uuid import uuid4

from scraper_utils import AGENT, MAX_BODY, host, policy_delay, progress, retry_after, same_page, web_url
from .extraction import PRODUCT_TILE_LINKS, inspect_html, product_records


PROJECT_DIR = Path(__file__).resolve().parents[1]
PRODUCT_LINKS = ('a[href*="/product/"], a[href*="/products/"], a[href*="/item/"], '
                 'a[href*="/items/"], a[href*="/rooms/"], a[href*="/dp/"], '
                 'a[href*="/pr/"][href*=".html"], '
                 'a[href*="/browse/product.do?pid="], ' + PRODUCT_TILE_LINKS)


def stop_tree(process, known=()):
    import psutil

    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    for child in set(children) | set(known):
        try:
            child.kill()
        except psutil.Error:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def run_worker(command, payload, timeout, memory_mb):
    import psutil

    with TemporaryFile() as input_file, TemporaryFile() as output:
        input_file.write(payload)
        input_file.seek(0)
        process = subprocess.Popen(command, stdin=input_file, stdout=output, stderr=subprocess.DEVNULL)
        known = set()
        limit = memory_mb * 1024 * 1024
        deadline = monotonic() + timeout
        reason = None
        try:
            while process.poll() is None:
                if monotonic() >= deadline:
                    reason = "BROWSER_TIMEOUT"
                    break
                try:
                    root = psutil.Process(process.pid)
                    children = root.children(recursive=True)
                    known.update(children)
                    rss = 0
                    for member in [root, *children]:
                        try:
                            rss += member.memory_info().rss
                        except psutil.Error:
                            continue
                    if limit and rss > limit:
                        reason = "MEMORY_LIMIT"
                        break
                except psutil.Error:
                    pass
                sleep(.2)
        finally:
            stop_tree(process, known)
        output.seek(0)
        return reason or ("BROWSER_CRASH" if process.returncode else None), output.read(32_000_001)


def save_error_diagnostic(crawler):
    if crawler.stats["records"] or crawler.screenshots or not crawler.errors:
        return
    try:
        payload = json.dumps({"diagnostic": crawler.errors[0]}).encode()
        reason, output = run_worker(
            [sys.executable, str(PROJECT_DIR / "universal_scraper.py"), "--internal-render"], payload,
            min(crawler.options.browser_timeout, 12), crawler.options.browser_memory_mb,
        )
        if reason:
            progress(f"Error screenshot unavailable: {reason}")
            return
        screenshot = json.loads(output)
        crawler.screenshots.append(screenshot)
        crawler.stats["screenshots_saved"] += 1
        progress(f"Error diagnostic screenshot: {screenshot['path']}")
    except (ImportError, OSError, ValueError, KeyError):
        progress("Error screenshot unavailable. See the crawl error above.")


def render_error_diagnostic(error):
    from playwright.sync_api import sync_playwright

    directory = PROJECT_DIR / "screenshots"
    directory.mkdir(exist_ok=True)
    path = directory / f"error-{uuid4().hex[:12]}.png"
    fields = "".join(
        f"<dt>{label}</dt><dd>{escape(str(error[key])[:1000])}</dd>"
        for label, key in (("Stage", "stage"), ("URL", "url"), ("Error", "message"))
    )
    with sync_playwright() as playwright, playwright.chromium.launch() as browser:
        page = browser.new_page(viewport={"width": 1100, "height": 450})
        page.route("**/*", lambda route: route.abort())
        page.set_content(
            "<style>body{font:18px system-ui;padding:36px;color:#182230}"
            "dt{font-weight:bold;margin-top:16px}dd{margin:4px 0;overflow-wrap:anywhere}</style>"
            "<h1>Scraper error diagnostic</h1><p>Generated locally from the crawl log.</p>"
            f"<dl>{fields}</dl>"
        )
        page.screenshot(path=path, full_page=True, timeout=3000)
    return {"url": error["url"], "reason": error["category"], "kind": "diagnostic", "path": str(path)}


def render(crawler, url, *, discovery):
    error_start = len(crawler.errors)
    records, links = render_worker(crawler, url, discovery=discovery)
    useful = (any(host(row["url"]) in crawler.scope_hosts for row in records)
              or any(rank == 1 and host(link) in crawler.scope_hosts for rank, link in links)) if discovery else any(
        same_page(row["url"], url) and not row["method"].endswith(":listing_card") for row in records
    )
    if (useful or crawler.stats["chrome_fallback_attempted"] or crawler.stats["rate_limited"]
            or any(error["category"] in {"HTTP_403", "HTTP_429", "ROBOTS_DISALLOWED",
                                         "ROBOTS_ACCESS_UNKNOWN", "REDIRECT_OUT_OF_SCOPE"}
                   or 400 <= error.get("status", 0) < 500 for error in crawler.errors[error_start:])):
        return records, links
    policy = crawler.policy(url)
    if policy is None or not policy.can_fetch(url, AGENT):
        return records, links
    crawler.pace(url, policy_delay(policy))
    crawler.stats["chrome_fallback_attempted"] += 1
    progress(f"Trying installed Chrome in headless mode: {url}")
    alternate_records, alternate_links = render_worker(crawler, url, discovery=discovery, installed_chrome=True)
    return records + alternate_records, links + alternate_links


def render_worker(crawler, url, *, discovery, installed_chrome=False):
    options = {key: getattr(crawler.options, key, None) for key in (
        "delay", "timeout", "retries", "max_retry_wait", "ca_bundle", "allow_hosts",
        "rules", "wait_selector", "render_wait",
    )}
    options["robots_cache"] = str(crawler.options.robots_cache) if crawler.options.robots_cache else None
    payload = json.dumps(dict(seed=crawler.seed, url=url, discovery=discovery,
                              installed_chrome=installed_chrome, options=options)).encode()
    attempts = 1 if installed_chrome else 2
    for attempt in range(attempts):
        reason, output = run_worker(
            [sys.executable, str(PROJECT_DIR / "universal_scraper.py"), "--internal-render"], payload,
            crawler.options.browser_timeout, crawler.options.browser_memory_mb,
        )
        if not reason:
            try:
                result = json.loads(output)
                crawler.errors.extend(result["errors"])
                crawler.redirects.extend(result["redirects"])
                crawler.robot_status.update({key: value for key, value in result["robots"].items()
                                          if key not in crawler.robot_status})
                crawler.stats["rate_limited"] += result["rate_limited"]
                crawler.stats["browser_errors"] += bool(result["errors"])
                for screenshot in result["screenshots"]:
                    crawler.screenshots.append(screenshot)
                    crawler.stats["screenshots_saved"] += 1
                    progress(f"Browser screenshot ({screenshot['reason']}): {screenshot['path']}")
                if attempt + 1 < attempts and any(error["category"] == "BROWSER_CRASH" for error in result["errors"]):
                    continue
                return result["records"], result["links"]
            except (ValueError, KeyError, TypeError):
                reason = "BROWSER_CRASH"
        crawler.issue(reason, url, "browser", "Isolated Chromium worker failed")
        crawler.stats["browser_errors"] += 1
        if reason != "BROWSER_CRASH":
            break
    return [], []


def wait_for_product_content(crawler, page, discovery, timeout_error, *, installed_chrome=False):
    wait = crawler.options.render_wait
    if wait is None:
        wait = (16 if installed_chrome else 12) if discovery else (8 if installed_chrome else 5)
    deadline = monotonic() + wait
    selector = crawler.options.wait_selector

    def wait_until_ready(seconds):
        if selector or discovery:
            page.locator(selector or PRODUCT_LINKS).first.wait_for(state="attached", timeout=seconds * 1000)
        else:
            page.wait_for_function(r"""() => {
                if (document.querySelector('[itemtype*="schema.org/Product"], meta[property="og:type"][content="product"], meta[property="product:price:amount"]')) {
                    return true;
                }
                return Array.from(document.querySelectorAll('script[type="application/ld+json"]')).some(
                    script => /"@type"\s*:\s*(?:\[\s*)?"(?:https?:\/\/schema\.org\/)?(?:Product(?:Group|Model)?|VacationRental|Accommodation|HotelRoom|Apartment|House)"/.test(script.textContent)
                );
            }""", timeout=seconds * 1000)

    if wait <= 0:
        return
    try:
        wait_until_ready(min(2.5, wait) if discovery else wait)
        return
    except timeout_error:
        if not discovery or monotonic() >= deadline:
            return
    scroll = """() => {
        const grid = document.querySelector('.product-grid, .product-tile, [data-pid]');
        if (grid && grid.getBoundingClientRect().top >= window.innerHeight) {
            grid.scrollIntoView({block: 'center'});
        } else {
            window.scrollBy(0, Math.round(window.innerHeight * 0.75));
        }
    }"""
    for step in range(2 if installed_chrome else 1):
        page.evaluate(scroll)
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        try:
            wait_until_ready(min(remaining, 5) if installed_chrome and step == 0 else remaining)
            return
        except timeout_error:
            if monotonic() >= deadline:
                return


def render_local(crawler, url, *, discovery, installed_chrome=False):
    try:
        from playwright.sync_api import Error, TimeoutError as BrowserTimeout, sync_playwright
    except ImportError:
        print("Browser fallback unavailable. Install requirements and Playwright Chromium.", file=sys.stderr)
        return [], []
    try:
        playwright = crawler.stack.enter_context(sync_playwright())
        browser = (playwright.chromium.launch(channel="chrome", headless=True)
                   if installed_chrome else playwright.chromium.launch())

        def close_browser():
            with suppress(Error):
                browser.close()

        crawler.stack.callback(close_browser)
        context = browser.new_context(user_agent=AGENT, service_workers="block")
        page = context.new_page()
    except Error as exc:
        crawler.issue("BROWSER_CRASH", url, "browser", exc)
        progress(f"Cannot open browser page for {url}: {exc}")
        return [], []
    responses = []

    def save_screenshot(reason):
        try:
            directory = PROJECT_DIR / "screenshots"
            directory.mkdir(exist_ok=True)
            path = directory / f"{reason}-{uuid4().hex[:12]}.png"
            page.screenshot(path=path, timeout=3000)
            crawler.screenshots.append({"url": url, "rendered_url": page.url,
                                     "reason": reason, "path": str(path)})
        except (Error, OSError):
            pass

    def capture(response):
        if (len(responses) < 12 and response.ok and host(response.url) in crawler.scope_hosts
                and response.request.resource_type in {"xhr", "fetch"}
                and "json" in response.headers.get("content-type", "")):
            responses.append(response)

    def route_request(route):
        request = route.request
        if request.method not in {"GET", "HEAD"}:
            route.abort()
            return
        if request.is_navigation_request() or request.resource_type in {"xhr", "fetch"}:
            policy = crawler.policy(request.url) if host(request.url) in crawler.scope_hosts else None
            if policy is None or not policy.can_fetch(request.url, AGENT):
                route.abort()
                return
            # Playwright only routes the first request in a redirect chain. Inspect
            # the response without following redirects on browser-managed requests.
            try:
                response = route.fetch(max_redirects=0, timeout=crawler.options.timeout * 1000)
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    target = web_url(request.url, location)
                    crawler.redirects.append({"from": request.url, "to": target, "status": response.status})
                    if request.is_navigation_request():
                        category = "BROWSER_REDIRECT" if target and host(target) in crawler.scope_hosts else "REDIRECT_OUT_OF_SCOPE"
                        crawler.issue(category, request.url, "browser", "Browser redirect stopped for policy validation")
                    route.abort()
                else:
                    route.fulfill(response=response)
            except Error:
                with suppress(Error):
                    route.abort()
            finally:
                if "response" in locals():
                    with suppress(Error):
                        response.dispose()
            return
        if request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    try:
        page.on("response", capture)
        page.route("**/*", route_request)
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        if response is None:
            save_screenshot("no-response")
            return [], []
        if not response.ok:
            crawler.stats["browser_errors"] += 1
            code = response.status
            category = f"HTTP_{code}" if code in {403, 429} else "HTTP_5XX" if code >= 500 else "BROWSER_HTTP_ERROR"
            crawler.issue(category, url, "browser", f"HTTP {code}", status=code,
                       retry_after=retry_after(response.headers.get("retry-after")))
            if code == 429:
                crawler.stats["rate_limited"] += 1
            progress(f"Browser HTTP {response.status} for {url}")
            save_screenshot(f"http-{code}")
            return [], []
        wait_for_product_content(crawler, page, discovery, BrowserTimeout, installed_chrome=installed_chrome)
        source = web_url(url, page.url, clean=True) or url
        policy = crawler.policy(source) if host(source) in crawler.scope_hosts else None
        if policy is None or not policy.can_fetch(source, AGENT):
            save_screenshot("navigation-unverified")
            return [], []
        transport = "chrome" if installed_chrome else "browser"
        records, links = inspect_html(page.content(), source, transport, crawler.options.rules)
        for payload in responses:
            try:
                if not crawler.can_fetch(payload.url):
                    continue
                body = payload.body()
                if len(body) <= MAX_BODY:
                    records.extend(product_records(json.loads(body), source, f"{transport}:network_json"))
            except (Error, ValueError, RecursionError):
                continue
        if not records and (not discovery or not any(
            rank == 1 and host(link) in crawler.scope_hosts for rank, link in links
        )):
            save_screenshot("no-products")
        return records, links
    except Error as exc:
        crawler.stats["browser_errors"] += 1
        crawler.issue("BROWSER_TIMEOUT" if isinstance(exc, BrowserTimeout) else "BROWSER_CRASH", url, "browser", exc)
        print(f"Browser error for {url}: {exc}", file=sys.stderr)
        save_screenshot("browser-error")
        return [], []
    finally:
        with suppress(Error):
            page.close()
