"""Resolve source filters and read configured CSS fields from HTML."""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from scraper_utils import host, price_number, web_url


LIST_FIELDS = {"pictures", "colors", "sizes", "payment_options"}
URL_FIELDS = {"product_url", "brand_url", "logo", "pictures", "video_url"}


def source_url(target, source):
    base = target["url"]
    url = web_url(base, source.get("path") or base)
    if not url or host(url) != host(base):
        raise ValueError("Source path must stay on the configured website")
    params = dict(source.get("params", {}))
    if target.get("query_param") and source.get("q"):
        params.setdefault(target["query_param"], source["q"])
    return with_query(url, params)


def with_query(url, params):
    if not params:
        return url
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in params]
    query.extend(params.items())
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def structured_nodes(soup, rule):
    for script in soup.select(rule["css"]):
        try:
            nodes = [json.loads(script.get_text())]
        except (ValueError, RecursionError):
            continue
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                kinds = node.get("@type", [])
                if isinstance(kinds, str):
                    kinds = [kinds]
                if isinstance(kinds, list) and any(
                    isinstance(kind, str) and kind.rsplit("/", 1)[-1] in rule["types"] for kind in kinds
                ):
                    yield node
                nodes.extend(reversed(list(node.values())))
            elif isinstance(node, list):
                nodes.extend(reversed(node))


def listing_links(html, url, selectors):
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    drop = selectors.get("drop_product_query", ())

    def collect(values):
        for value in values:
            link = web_url(url, value, clean=True)
            if not link or host(link) != host(url):
                continue
            if drop:
                parts = urlsplit(link)
                query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in drop]
                link = urlunsplit(parts._replace(query=urlencode(query)))
            links.setdefault(link, None)

    collect(node.get("href") for node in soup.select(selectors["product_links"]))
    if not links and (structured := selectors.get("structured_links")):
        collect(node.get("url") for node in structured_nodes(soup, structured))
    selector = selectors.get("next_page")
    next_link = soup.select_one(selector) if selector else None
    next_url = web_url(url, next_link.get("href"), clean=True) if next_link else None
    return list(links), next_url if next_url and host(next_url) == host(url) else None


def read_field(soup, rule, field, url):
    if isinstance(rule, str):
        rule = {"css": rule}
    many = rule.get("many", field in LIST_FIELDS)
    values = []
    if json_path := rule.get("json_path"):
        raw = []
        for node in structured_nodes(soup, rule):
            value = node
            for key in json_path.split("."):
                if isinstance(value, list):
                    value = value[0] if value else None
                value = value.get(key) if isinstance(value, dict) else None
            if value is not None:
                raw = value if isinstance(value, list) else [value]
                break
    else:
        raw = (node.get(rule["attr"]) if rule.get("attr") else node.get_text(" ", strip=True)
               for node in soup.select(rule["css"]))
    for value in raw:
        if type(value) in {int, float}:
            value = str(value)
        if not isinstance(value, str) or not value.strip():
            continue
        if (rule.get("contains") and rule["contains"] not in value
                or rule.get("exclude") and rule["exclude"] in value):
            continue
        value = web_url(url, value) if rule.get("url", field in URL_FIELDS) else value.strip()
        if value and value not in values:
            values.append(value)
        if values and not many:
            return values[0]
    if not values and (fallback := rule.get("fallback")):
        return read_field(soup, fallback, field, url)
    return values if many else None


def product_record(html, url, target, source):
    soup = BeautifulSoup(html, "html.parser")
    selectors = target["selectors"]
    fields = selectors["fields"]
    record = {}
    for field, rule in fields.items():
        if value := read_field(soup, rule, field, url):
            record[field] = value
    if not record or ("name" in fields and "name" not in record):
        return None
    if not isinstance(record.get("product_url"), str) or host(record["product_url"]) != host(url):
        record["product_url"] = url
    metadata = {
        "query": source.get("q"), "brand": target.get("brand"), "brand_url": target.get("brand_url"),
        "gender": source.get("gender"), "category": source.get("l_category"),
        "sub_category": source.get("l_subcategory"),
        "source_city": source.get("city"), "source_country": source.get("country"),
    }
    for key in selectors.get("metadata_fields", ()):
        if metadata.get(key) is not None:
            record.setdefault(key, metadata[key])
    if selectors.get("derive_brand_identity"):
        domain = urlsplit(record["product_url"]).netloc
        record["brand_url"] = domain
        record["logo"] = f"https://www.google.com/s2/favicons?sz=256&domain_url=https://{domain}"
    if not record.get("p_id") and (pattern := target.get("product_id_pattern")):
        match = re.search(pattern, urlsplit(record["product_url"]).path)
        if match:
            record["p_id"] = match.group(1)
    raw_price = record.get("price")
    symbol = re.search(r"[£€$¥]", raw_price) if isinstance(raw_price, str) else None
    if symbol:
        if "currency_sign" in fields:
            record.setdefault("currency_sign", symbol.group())
        if "currency" in fields and (currency := {"£": "GBP", "€": "EUR"}.get(symbol.group())):
            record.setdefault("currency", currency)
    for field in ("price", "orignal_price", "rating", "total_rating"):
        if isinstance(value := record.get(field), str):
            if field in {"price", "orignal_price"}:
                value = re.sub(r"^\s*(?:[A-Z]{3}|[£€$¥])\s*|\s*(?:[A-Z]{3}|[£€$¥])\s*$", "", value)
            record[field] = price_number(value)
    if not any(record.get(field) is not None for field in fields):
        return None
    return {key: value for key, value in record.items() if value is not None}
