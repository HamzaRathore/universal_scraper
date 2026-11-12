"""Extract and normalize products from storefront HTML and API responses."""

import json
import re
from urllib.parse import parse_qsl, urlsplit

from scraper_utils import (
    first, host, minor_price, objects, price_number, priority, room_url, same_page,
    scalar, schema_type, stock, utc_now, values, web_url,
)


PRODUCT_TILE_LINKS = '.product-tile a[href*=".html"], [data-pid] a[href*=".html"]'
ACCOMMODATION_TYPES = {"VacationRental", "Accommodation", "HotelRoom", "Apartment", "House"}
FIELDS = (
    "title", "brand", "description", "sku", "price", "currency",
    "availability", "images", "offers",
)


def detect_platform(html, headers):
    evidence = (html + " " + str(headers)).lower()
    markers = (
        ("shopify", ("cdn.shopify.com", "shopify-section", "x-shopify-")),
        ("woocommerce", ("/plugins/woocommerce/", "woocommerce-page", "wc-block-")),
        ("magento", ("magento_", "text/x-magento-init", "mage/cookies")),
        ("bigcommerce", ("cdn11.bigcommerce.com", "cdn8.bigcommerce.com", "bigcommerce.com/stencil")),
        ("salesforce-commerce-cloud", ("/on/demandware.", "demandware.static")),
    )
    return next((platform for platform, signals in markers if any(signal in evidence for signal in signals)), "custom")


def public_endpoint(platform, url, detail):
    parts = urlsplit(url)
    if parts.scheme != "https":
        return None
    if platform == "shopify" and detail and re.fullmatch(r"(?:/[^/]+)?/products/[^/]+/?", parts.path):
        return parts.path.rstrip("/") + ".js"
    if platform == "woocommerce" and not detail and parts.path == "/":
        return "/wp-json/wc/store/v1/products?per_page=12"
    return None


def shopify_currency(html):
    match = re.search(r'Shopify\.currency\s*=\s*\{[^}]{0,500}?"active"\s*:\s*"([A-Z]{3})"', html)
    return match.group(1) if match else None


def api_products(platform, payload, currency=None):
    """Convert documented public responses to the existing Schema.org extractor input."""
    if platform == "shopify" and isinstance(payload, dict) and isinstance(payload.get("title"), str):
        variants = payload.get("variants")
        if not isinstance(variants, list):
            return []
        return [{
            "@type": "Product",
            "name": payload["title"],
            "description": payload.get("description"),
            "brand": payload.get("vendor"),
            "category": payload.get("type"),
            "image": payload.get("images"),
            "offers": {"price": minor_price(payload.get("price"), 2) if currency else None,
                       "priceCurrency": currency, "availability": stock(payload.get("available"))},
            "variants": [{
                "sku": variant.get("sku"), "title": variant.get("title"),
                "price": minor_price(variant.get("price"), 2) if currency not in {None, ""} else None,
                "price_minor_units": variant.get("price") if type(variant.get("price")) is int else None,
                "originalPrice": minor_price(variant.get("compare_at_price"), 2) if currency else None,
                "currency": currency,
                "availability": stock(variant.get("available")),
                "options": [variant.get(key) for key in ("option1", "option2", "option3") if variant.get(key)],
            } for variant in variants if isinstance(variant, dict)],
        }]
    if platform == "woocommerce" and isinstance(payload, list):
        rows = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("permalink"), str):
                continue
            prices = item.get("prices")
            prices = prices if isinstance(prices, dict) else {}
            exponent = prices.get("currency_minor_unit")
            rows.append({
                "@type": "Product", "name": item.get("name"), "url": item["permalink"],
                "description": item.get("description"), "sku": item.get("sku"),
                "image": [image.get("src") for image in item.get("images") or [] if isinstance(image, dict)],
                "category": [category.get("name") for category in item.get("categories") or []
                             if isinstance(category, dict) and category.get("name")],
                "originalPrice": minor_price(prices.get("regular_price"), exponent),
                "offers": {"price": minor_price(prices.get("price"), exponent),
                           "priceCurrency": prices.get("currency_code"),
                           "availability": stock(item.get("is_in_stock"))},
            })
        return rows
    return []


def normalize_record(record, platform):
    raw = record.get("price")
    method = record["method"]
    currency = record.get("currency")
    record.update(
        price_raw=raw,
        price=price_number(raw),
        originalPrice=price_number(record.get("originalPrice")),
        currency=currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else None,
        variants=record.get("variants") or [],
        category=record.get("category"),
        sourceUrl=record["source_url"],
        extractionMethod=method,
        confidence=record.get("confidence", 0.95 if "structured_json" in method or method.startswith("api:") else
                              0.85 if "microdata" in method else 0.7 if "metadata" in method else 0.6),
        scrapedAt=utc_now(),
        platform=platform,
    )


def product_records(document, source, method):
    nodes = list(objects(document))
    by_id = {}
    for node in nodes:
        identity = node.get("@id")
        if isinstance(identity, str) and len(node) > len(by_id.get(identity, {})):
            by_id[identity] = node
    groups = {}
    populated_groups = set()
    for group in nodes:
        if not any(schema_type(kind) == "ProductGroup" for kind in values(group.get("@type"))):
            continue
        for variant in values(group.get("hasVariant")):
            if isinstance(variant, dict):
                ref = variant.get("@id")
                target = by_id.get(ref, variant) if isinstance(ref, str) else variant
                groups[id(target)] = group
    for node in nodes:
        parent = node.get("isVariantOf")
        if isinstance(parent, dict) and isinstance(parent.get("@id"), str):
            parent = by_id.get(parent["@id"], parent)
        if isinstance(parent, dict) and any(
            schema_type(kind) == "ProductGroup" for kind in values(parent.get("@type"))
        ):
            groups[id(node)] = parent
        group = groups.get(id(node))
        if (group and any(schema_type(kind) == "Product" for kind in values(node.get("@type")))
                and any(node.get(key) for key in ("offers", "sku", "productID"))):
            populated_groups.add(id(group))

    for node in nodes:
        kinds = {schema_type(kind) for kind in values(node.get("@type"))}
        accommodation = bool(ACCOMMODATION_TYPES & kinds)
        if not accommodation and not {"Product", "ProductGroup", "ProductModel"} & kinds:
            continue
        if id(node) in populated_groups:
            continue
        group = groups.get(id(node), {})
        concrete_variant = group and any(node.get(key) for key in ("offers", "sku", "productID"))
        title = scalar(node.get("name") or (group.get("name") if concrete_variant else None))
        if not isinstance(title, str):
            continue
        offers = []
        for offer in values(node.get("offers")):
            if not isinstance(offer, dict):
                continue
            ref = offer.get("@id")
            if isinstance(ref, str):
                offer = by_id.get(ref, {}) | offer
            price_spec = next((spec for spec in values(offer.get("priceSpecification"))
                               if isinstance(spec, dict) and scalar(spec.get("price")) is not None
                               and not (spec.get("priceType") or spec.get("validForMemberTier"))), {})
            original_price = next((scalar(spec.get("price")) for spec in values(offer.get("priceSpecification"))
                                   if isinstance(spec, dict)
                                   and schema_type(spec.get("priceType")) == "StrikethroughPrice"), None)
            entry = {
                "price": first(scalar(offer.get("price")), scalar(offer.get("lowPrice")),
                               scalar(price_spec.get("price"))),
                "high_price": scalar(offer.get("highPrice")),
                "original_price": original_price,
                "currency": first(scalar(offer.get("priceCurrency")), scalar(price_spec.get("priceCurrency"))),
                "availability": schema_type(offer["availability"]) if isinstance(offer.get("availability"), str) else None,
                "url": web_url(source, offer.get("url")),
            }
            if any(value is not None for value in entry.values()):
                offers.append(entry)
        images = []
        for image in values(node.get("image") or group.get("image")):
            if isinstance(image, dict):
                image = image.get("contentUrl") or image.get("url")
            if (url := web_url(source, image)) and url not in images:
                images.append(url)
        brand = next(iter(values(node.get("brand") or group.get("brand"))), None)
        if isinstance(brand, dict):
            ref = brand.get("@id")
            brand = brand.get("name") or (by_id.get(ref, {}).get("name") if isinstance(ref, str) else None)
        primary = offers[0] if offers else {}
        identity = node.get("@id")
        identity_url = identity if isinstance(identity, str) and identity.startswith(("http://", "https://", "/")) else None
        record_url = (web_url(source, node.get("url"), clean=True)
                      or (primary.get("url") if concrete_variant else None)
                      or web_url(source, identity_url, clean=True)
                      or primary.get("url") or source)
        listing_url = room_url(record_url)
        record = {
            "url": listing_url or record_url,
            "source_url": source,
            "method": method,
            "record_type": "accommodation" if accommodation or listing_url else "product",
            "title": title,
            "brand": scalar(brand),
            "description": scalar(node.get("description") or group.get("description")),
            "sku": first(scalar(node.get("sku")), scalar(node.get("productID")),
                         listing_url.rsplit("/", 1)[-1] if listing_url else None),
            "price": primary.get("price"),
            "currency": primary.get("currency"),
            "availability": primary.get("availability"),
            "images": images,
            "offers": offers,
            "category": node.get("category") if isinstance(node.get("category"), (str, list)) else None,
            "originalPrice": first(scalar(node.get("originalPrice")), primary.get("original_price")),
            "variants": node.get("variants") if method.startswith("api:") else [],
        }
        if group:
            for field in ("size", "color"):
                if (value := scalar(node.get(field))) is not None:
                    record[field] = value
            if (group_id := scalar(group.get("productGroupID"))) is not None:
                record["product_group_id"] = group_id
            record["variants"] = [{key: value for key in ("sku", "size", "color", "url")
                                   if (value := scalar(node.get(key))) is not None}]
        if record["record_type"] == "accommodation":
            detail_keys = ("address", "geo", "aggregateRating", "amenityFeature", "containsPlace",
                           "occupancy", "numberOfBedrooms", "numberOfBathroomsTotal",
                           "checkinTime", "checkoutTime")
            record["accommodation_details"] = {key: node[key] for key in detail_keys if key in node}
        yield record


def microdata(soup):
    for item in soup.select('[itemscope][itemtype*="schema.org/Product"]'):
        node = {"@type": "Product"}
        for tag in item.select("[itemprop]"):
            name = tag["itemprop"]
            parent = tag.find_parent(attrs={"itemscope": True})
            if parent is not item:
                continue
            if name in {"offers", "brand"} and tag.has_attr("itemscope"):
                node[name] = {
                    child["itemprop"]: element_value(child)
                    for child in tag.select("[itemprop]")
                    if child.find_parent(attrs={"itemscope": True}) is tag
                }
            elif name == "image":
                node.setdefault(name, []).append(element_value(tag))
            else:
                node.setdefault(name, element_value(tag))
        yield node


def element_value(tag):
    return next((tag[attr] for attr in ("content", "href", "src", "datetime")
                 if tag.has_attr(attr)), tag.get_text(" ", strip=True))


def dom_records(soup, source, transport, rules):
    for card in soup.select(rules["container"]):
        record = dict.fromkeys(FIELDS)
        for field, selector in rules["fields"].items():
            if field == "images":
                record[field] = [url for tag in card.select(selector)
                                 if (url := web_url(source, element_value(tag)))]
            elif tag := card.select_one(selector):
                record[field] = element_value(tag) or None
        if not record["title"]:
            continue
        record["url"] = web_url(source, record.get("url"), clean=True) or source
        record.update(source_url=source, method=f"{transport}:selectors", offers=[], record_type="product",
                      images=record["images"] or [])
        yield record


def product_price_record(soup, source, transport):
    if priority(source) != 1 or not (heading := soup.select_one("h1")):
        return None
    title = heading.get_text(" ", strip=True)
    if not title:
        return None
    prices = soup.select('[itemprop="price"], [data-price-amount], .prices .price, .prices [content]')
    for tag in prices:
        price = tag.get("data-price-amount") or element_value(tag)
        if price_number(price) is not None:
            record = dict.fromkeys(FIELDS)
            record.update(url=source, source_url=source, method=f"{transport}:selectors",
                          record_type="product", title=title, price=price, images=[], offers=[])
            return record
    return None


def linked_room_cards(soup, source, transport):
    seen = set()
    for anchor in soup.select('a[href*="/rooms/"]'):
        url = room_url(web_url(source, anchor["href"]) or "")
        if not url or host(url) != host(source) or url in seen:
            continue
        card = anchor.find_parent(attrs={"data-testid": re.compile(r"card-container|listing-card$")})
        title_selector = '[data-testid$="card-title"], [data-testid$="listing-title"], h2, h3, h4'
        if card is None:
            for parent in [anchor, *list(anchor.parents)[:5]]:
                if any(room_url(web_url(source, link["href"]) or "") not in {None, url}
                       for link in parent.select('a[href*="/rooms/"]')):
                    break
                if parent.select_one(title_selector):
                    card = parent
                    break
        if card is None:
            continue
        title_tag = card.select_one(title_selector)
        title = title_tag.get_text(" ", strip=True) if title_tag else anchor.get("aria-label")
        if not title:
            continue
        text = card.get_text(" ", strip=True)
        quote = re.search(r"(?:[A-Z]{3}\s+|[£€$])\s*\d[\d,.]*(?:\s+for\s+\d+\s+nights?)?", text)
        images = []
        for image in card.select("img[src]"):
            image_url = web_url(source, image["src"])
            if image_url and image_url not in images:
                images.append(image_url)
        record = dict.fromkeys(FIELDS)
        record.update(url=url, source_url=source, method=f"{transport}:listing_card",
                      record_type="accommodation", title=title, sku=url.rsplit("/", 1)[-1],
                      price_display=quote.group() if quote else None, images=images, offers=[])
        seen.add(url)
        yield record


def inspect_html(html, source, transport, rules=None):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    records = []
    for script in soup.find_all("script", type=lambda value: value and
                                value.split(";", 1)[0].strip().lower() in
                                {"application/ld+json", "application/json"}):
        try:
            document = json.loads(script.string or script.get_text())
        except (ValueError, RecursionError):
            continue
        records.extend(product_records(document, source, f"{transport}:structured_json"))

    if not records:
        records.extend(product_records(list(microdata(soup)), source, f"{transport}:microdata"))

    meta = {}
    for tag in soup.find_all("meta", content=True):
        key = tag.get("property") or tag.get("name")
        if key:
            meta.setdefault(key.lower(), tag["content"])
    canonical = soup.find("link", rel="canonical", href=True)
    canonical_url = web_url(source, canonical["href"], clean=True) if canonical else None
    declared_url = web_url(source, meta.get("og:url"), clean=True) or canonical_url
    if canonical_url and len(records) == 1 and urlsplit(canonical_url).path == "/browse/product.do":
        # The canonical PID identifies the parent, while the offer URL can use a SKU suffix.
        row = records[0]
        pid = dict(parse_qsl(urlsplit(canonical_url).query)).get("pid")
        variant_url = row["url"]
        variant_pid = dict(parse_qsl(urlsplit(variant_url).query)).get("pid", "")
        if (pid and str(row["sku"]) == pid and same_page(canonical_url, source)
                and host(variant_url) == host(source) and variant_pid.startswith(pid)):
            row.update(url=canonical_url, variant_url=variant_url)
    if (len(records) == 1 and declared_url and meta.get("og:type") == "product"
            and records[0]["record_type"] == "product"
            and records[0]["title"] == meta.get("og:title")):
        record_url = records[0]["url"]
        # A detail page can publish a base canonical URL and one size or color variant.
        directories = {
            (host(url), urlsplit(url).path.rsplit("/", 1)[0])
            for url in (source, declared_url, record_url)
        }
        if len(directories) == 1 and not same_page(record_url, source):
            records[0].update(url=source, variant_url=record_url)
    listing_url = room_url(source)
    listing_metadata = listing_url is not None and declared_url is not None and room_url(declared_url) == listing_url
    if not records and (listing_metadata or meta.get("og:type", "").startswith("product")
                        or "product:price:amount" in meta):
        title = meta.get("og:title") or (soup.title.get_text(strip=True) if soup.title else None)
        if title:
            image = web_url(source, meta.get("og:image"))
            records.append({
                "url": listing_url or declared_url or source,
                "source_url": source, "method": f"{transport}:metadata",
                "record_type": "accommodation" if listing_metadata else "product",
                "title": title, "brand": meta.get("product:brand"),
                "description": meta.get("og:description") or meta.get("description"),
                "sku": listing_url.rsplit("/", 1)[-1] if listing_metadata else meta.get("product:retailer_item_id"),
                "price": meta.get("product:price:amount"),
                "currency": meta.get("product:price:currency"), "availability": meta.get("product:availability"),
                "images": [image] if image else [], "offers": [],
            })

    if not records:
        if rules:
            records.extend(dom_records(soup, source, transport, rules))
        elif record := product_price_record(soup, source, transport):
            records.append(record)
    if not room_url(source):
        records.extend(linked_room_cards(soup, source, transport))
        if any(row["record_type"] == "accommodation" and host(row["url"]) == host(source)
               and room_url(row["url"]) for row in records):
            records = [row for row in records if not (
                row["record_type"] == "product" and same_page(row["url"], source)
                and not (row["sku"] or row["offers"])
            )]

    links = []
    tile_links = set(map(id, soup.select(PRODUCT_TILE_LINKS)))
    card_links = set(map(id, soup.select(".product-item a[href], .product-card a[href], .grid-product a[href], .product-tile a[href]")))
    card_targets = set()
    if declared_url and priority(declared_url) == 1 and (
        meta.get("og:type") == "product" or any(row["record_type"] == "product" for row in records)
    ):
        links.append((1, declared_url))
    for tag in soup.find_all(["a", "link"], href=True):
        next_page = "next" in tag.get("rel", [])
        if tag.name == "a" or next_page:
            url = web_url(source, tag["href"], clean=True)
            if url and (rank := priority(url)) is not None:
                if rank == 3 and id(tag) in tile_links and host(url) == host(source):
                    target_path = urlsplit(url).path
                    if target_path.endswith(".html") and target_path.count("/") >= 2:
                        rank = 1
                if rank == 1 and id(tag) in card_links and host(url) == host(source):
                    card_targets.add(room_url(url) or url)
                if not next_page:
                    target = urlsplit(url)
                    next_page = (target.netloc == urlsplit(source).netloc
                                 and target.path == urlsplit(source).path
                                 and any(key in {"page", "pagenumber", "offset", "cursor"}
                                         for key, _ in parse_qsl(target.query)))
                links.append((0 if next_page else rank, room_url(url) or url))
    if card_targets and priority(source) != 1:
        links = [(rank, url) for rank, url in links if rank != 1 or url in card_targets or url == declared_url]
    return records, links
