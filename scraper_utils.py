"""Shared URL, robots, and price helpers."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
import os
import re
import sys
from tempfile import NamedTemporaryFile
from time import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


AGENT = "UniversalScraper"
MAX_BODY = 12_000_000
TRACKING_KEYS = {"gclid", "fbclid", "msclkid"}


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
            if filtered != params:
                query = urlencode(filtered)
        return urlunsplit((url.scheme, url.netloc.lower(), url.path or "/", query, ""))
    except ValueError:
        return None


def host(url):
    return urlsplit(url).netloc.removeprefix("www.")


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
