"""Shared URL, robots policy, and data normalization helpers."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
import re
import ssl
import sys
from tempfile import NamedTemporaryFile
from time import time
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


AGENT = "UniversalScraper"
MAX_BODY = 12_000_000
TRACKING_KEYS = {"gclid", "fbclid", "msclkid"}
PRODUCT_PATHS = {"product", "products", "product-detail", "p", "dp", "item", "items", "pd"}
PRODUCT_ID_SUFFIX = re.compile(r".+-p\d{6,}(?:\.html)?")
PRODUCT_SHORT_ROUTE = re.compile(r".+-(?=[a-z0-9]*\d)[a-z0-9]{5,}\.html")
CATEGORY_ID_SUFFIX = re.compile(r".+-l\d+\.html")
PRODUCT_STYLE_ROUTE = re.compile(r"(?:/[a-z]{2}(?:-[a-z]{2})?){0,2}/t/[^/]+(?:/[^/]+)?/?")
SHOP_PATHS = {"shop", "store", "collections", "category", "categories", "catalog"}
SKIP_PATHS = {"cart", "checkout", "login", "account", "register", "wishlist", "search"}


class ScopeError(ValueError):
    pass


def retry_after(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def policy_delay(policy):
    rate = policy.request_rate(AGENT)
    rate_delay = rate.seconds / rate.requests if rate and rate.requests > 0 else 0
    return max(policy.crawl_delay(AGENT) or 0, rate_delay)


def error_category(error, stage):
    reason = getattr(error, "reason", error)
    if isinstance(error, ScopeError):
        return "REDIRECT_OUT_OF_SCOPE"
    if isinstance(reason, ssl.SSLError):
        return "TLS_ERROR"
    if isinstance(reason, TimeoutError):
        return "ROBOTS_TIMEOUT" if stage == "robots" else "HTTP_TIMEOUT"
    if isinstance(error, HTTPError):
        if error.code in {403, 429}:
            return f"HTTP_{error.code}"
        if 500 <= error.code < 600:
            return "HTTP_5XX"
    return "ROBOTS_ACCESS_UNKNOWN" if stage == "robots" else "HTTP_ERROR"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = file.name
        try:
            json.dump(data, file, ensure_ascii=False)
            file.close()
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class RobotsCache:
    def __init__(self, directory):
        self.directory = directory

    def path(self, origin):
        return self.directory / (sha256(origin.encode()).hexdigest() + ".json")

    def read(self, origin):
        if self.directory is None:
            return None
        try:
            data = json.loads(self.path(origin).read_text(encoding="utf-8"))
            if (data["origin"] == origin and isinstance(data["rules"], str)
                    and time() < data["expires"]):
                return data["rules"]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None

    def save(self, origin, rules, cache_control=""):
        if self.directory is None:
            return
        directives = dict(part.strip().lower().partition("=")[::2] for part in cache_control.split(","))
        ttl = 86400
        if "max-age" in directives:
            ttl = min(ttl, retry_after(directives["max-age"].strip('"')))
        try:
            if not ttl or "no-store" in directives or "no-cache" in directives:
                self.path(origin).unlink(missing_ok=True)
            else:
                write_json(self.path(origin), {"origin": origin, "rules": rules, "expires": time() + ttl})
        except OSError:
            pass


def web_url(base, value, *, clean=False):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value = value.strip()
        origin = urlsplit(base)
        prefix, separator, _ = value.partition("/")
        if separator and prefix and prefix.lower().removeprefix("www.") == (origin.hostname or "").removeprefix("www."):
            value = f"{origin.scheme}://{value}"
        url = urlsplit(urljoin(base, value))
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or any(character.isspace() for character in url.netloc)):
            return None
        _ = url.port  # Validate explicit ports.
        query = url.query
        if clean and query:
            params = parse_qsl(query, keep_blank_values=True)
            filtered = [(key, val) for key, val in params
                        if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")]
            if url.path.lower().endswith(".html"):
                filtered = [(key, val) for key, val in filtered if (key, val) != ("source", "plp")]
            if filtered != params:
                query = urlencode(filtered)
        return urlunsplit((url.scheme, url.netloc.lower(), url.path or "/", query, ""))
    except ValueError:
        return None


def host(url):
    return urlsplit(url).netloc.removeprefix("www.")


def tagged_url(base, target):
    if target.startswith("?"):
        parts = urlsplit(base)
        extra = parse_qsl(target[1:], keep_blank_values=True)
        keys = {key for key, _ in extra}
        original = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if key not in keys]
        target = urlunsplit(parts._replace(query=urlencode(original + extra)))
    url = web_url(base, target or base, clean=True)
    if not url or host(url) != host(base):
        raise ValueError(f"Tag target must stay on the configured storefront: {target}")
    return url


def room_url(url):
    parts = urlsplit(url)
    segments = parts.path.strip("/").split("/")
    if len(segments) == 2 and segments[0] == "rooms" and segments[1].isdigit():
        return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join(segments), "", ""))
    return None


def same_page(left, right):
    first_url, second_url = urlsplit(left), urlsplit(right)
    if host(left) != host(right) or first_url.path.rstrip("/") != second_url.path.rstrip("/"):
        return False
    if first_url.path == "/browse/product.do":
        return dict(parse_qsl(first_url.query)).get("pid") == dict(parse_qsl(second_url.query)).get("pid")
    return True


def priority(url):
    path = urlsplit(url).path.lower()
    parts = set(path.split("/"))
    if parts & SKIP_PATHS or path.endswith((".pdf", ".zip", ".jpg", ".png", ".webp", ".svg", ".css", ".js")):
        return None
    segments = path.strip("/").split("/")
    if path == "/browse/product.do" and dict(parse_qsl(urlsplit(url).query)).get("pid"):
        return 1
    if set(segments[:-1]) & PRODUCT_PATHS or room_url(url):
        return 1
    if (PRODUCT_ID_SUFFIX.fullmatch(segments[-1])
            or (segments[-2:-1] == ["pr"] and PRODUCT_SHORT_ROUTE.fullmatch(segments[-1]))):
        return 1
    if PRODUCT_STYLE_ROUTE.fullmatch(path):
        return 1
    if parts & SHOP_PATHS or CATEGORY_ID_SUFFIX.fullmatch(segments[-1]):
        return 2
    return 3


def values(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def objects(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            stack.extend(reversed(item))


def schema_type(value):
    return str(value).rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def scalar(value):
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def first(*items):
    return next((item for item in items if item is not None), None)


def minor_price(value, exponent):
    if type(exponent) is int and 0 <= exponent <= 6 and re.fullmatch(r"\d{1,20}", str(value)):
        return str(Decimal(str(value)).scaleb(-exponent))
    return None


def stock(value):
    return {True: "InStock", False: "OutOfStock"}.get(value) if isinstance(value, bool) else None


def price_number(value):
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    # Normalize common grouping conventions while leaving ambiguous formats unchanged.
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{1,2}", text):
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", text):
        text = text.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d{1,2})?", text):
        text = re.sub(r"\s", "", text).replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?", text) or len(text) > 25:
        return None
    try:
        number = Decimal(text)
        return int(number) if number == number.to_integral_value() else float(number)
    except InvalidOperation:
        return None


def progress(message):
    print(message, file=sys.stderr, flush=True)
