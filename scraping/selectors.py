"""CSS selector profiles for storefronts."""

# Keep the registry active when switching profiles.
SELECTOR_PROFILES = {}

SELECTOR_PROFILES["vinted"] = {
    "product_links": 'a[data-testid$="--overlay-link"][href*="/items/"]',
    "next_page": 'a[rel="next"], a[data-testid="pagination-arrow-right"]',
    "drop_product_query": {"referrer"},
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "gender", "category", "sub_category"),
    "fields": {
        "name": "h1",
        "price": '[data-testid="item-price"]',
        "description": '[itemprop="description"]',
        "condition": '[itemprop="status"]',
        "sub_brand": '[itemprop="brand"]',
        "sizes": '[itemprop="size"]',
        "colors": '[itemprop="color"]',
        "pictures": {
            "css": ('img.web_ui__Image__content[data-testid^="item-photo-"][data-testid$="--img"], '
                    '[data-testid="item-photos"] img.web_ui__Image__content, '
                    '.item-photos img.web_ui__Image__content, '
                    '.item-photos-thumbnails-container img.web_ui__Image__content'),
            "attr": "src",
            "contains": "/t/",
            "exclude": "310x430",
            "fallback": {"css": 'meta[property="og:image"]', "attr": "content",
                         "contains": "/t/", "exclude": "310x430"},
        },
        "currency": {"css": 'meta[itemprop="priceCurrency"], meta[property="product:price:currency"]', "attr": "content"},
    },
}
SELECTOR_PROFILES["khaadi"] = {
    "product_links": '.product-tile a[href*=".html"], [data-pid] a[href*=".html"]',
    "drop_product_query": {"source"},
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "gender", "category", "sub_category"),
    "fields": {
        "name": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "name"},
        "price": {"css": ".sales .cc-price[content]", "attr": "content"},
        "currency": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.priceCurrency"},
        "description": {"css": 'meta[name="description"]', "attr": "content"},
        "pictures": {"css": 'meta[property="og:image"]', "attr": "content", "exclude": "Logo-OG"},
    },
}

SELECTOR_PROFILES["breakout"] = {
    "product_links": '.product-item a[href*="/products/"]',
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "gender", "category", "sub_category"),
    "fields": {
        "name": "h1",
        "p_id": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "sku"},
        "price": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.price"},
        "currency": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.priceCurrency"},
        "description": {"css": 'meta[property="og:description"]', "attr": "content"},
        "pictures": {"css": 'div.pointer-events-none > img.h-full.w-auto.object-contain[src]', "attr": "src"},
    },
}

SELECTOR_PROFILES["alexandermcqueen"] = {
    "product_links": 'a[href*="/pr/"][href*=".html"]',
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "gender", "category", "sub_category"),
    "fields": {
        "name": "h1",
        "price": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.price"},
        "currency": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.priceCurrency"},
        "description": {"css": 'meta[property="og:description"]', "attr": "content"},
        "pictures": {"css": 'meta[property="og:image"]', "attr": "content"},
    },
}

SELECTOR_PROFILES["lama"] = {
    "product_links": '.grid-product a[href^="/products/"]',
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "gender", "category", "sub_category"),
    "fields": {
        "name": "h1",
        "price": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.price"},
        "currency": {"css": 'script[type="application/ld+json"]', "types": {"Product"}, "json_path": "offers.priceCurrency"},
        "description": {"css": 'meta[property="og:description"]', "attr": "content"},
        "pictures": {"css": 'meta[property="og:image"]', "attr": "content"},
    },
}

OLX_PRODUCT = {"css": 'script[type="application/ld+json"]', "types": {"Product"}}

SELECTOR_PROFILES["olx"] = {
    "product_links": 'a[href^="/item/"][href*="-iid-"][title]',
    "next_page": 'a[rel="next"]',
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "category", "source_city"),
    "fields": {
        "name": '[aria-label="Overview"] h1',
        "description": {"css": '[aria-label="Description"] ._7a99ad24',
                        "fallback": {**OLX_PRODUCT, "json_path": "description"}},
        "price": {**OLX_PRODUCT, "json_path": "offers.price",
                  "fallback": {"css": '[aria-label="Overview"] [aria-label="Price"]',
                               "pattern": r"^Rs\.?\s*([\d,]+(?:\.\d+)?)\s*$"}},
        "currency": {**OLX_PRODUCT, "json_path": "offers.priceCurrency"},
        "pictures": {"css": '.image-gallery-slides img[src]', "attr": "src",
                     "fallback": {**OLX_PRODUCT, "json_path": "image"}},
        "location": '[aria-label="Overview"] [aria-label="Location"]',
        "listed_at": '[aria-label="Overview"] [aria-label="Creation date"]',
        "item_type": '[aria-label="Details"] ._0272c9dc > span:-soup-contains-own("Type") + span',
        "sub_brand": '[aria-label="Details"] ._0272c9dc > span:-soup-contains-own("Brand") + span',
        "condition": '[aria-label="Details"] ._0272c9dc > span:-soup-contains-own("Condition") + span',
        "seller_name": {**OLX_PRODUCT, "json_path": "offers.seller.name"},
        "seller_url": {"css": 'a[href^="/profile/"]:has(span:-soup-contains-own("Posted by"))',
                       "attr": "href", "url": True},
    },
}

DARAZ_PRODUCT = {"css": 'script[type="application/ld+json"]', "types": {"Product"}}

SELECTOR_PROFILES["daraz"] = {
    "product_links": '[data-qa-locator="product-item"] a[href*="/products/"][href*=".html"]',
    "json_listing": {
        "items": "mods.listItems", "url": "itemUrl",
        "page": "mainInfo.page", "size": "mainInfo.pageSize",
        "total": "mainInfo.totalResults", "page_param": "page",
        "offer": {"price": "price", "orignal_price": "originalPrice", "discount_percentage": "discount"},
    },
    "next_page": 'a[rel="next"]',
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "category"),
    "fields": {
        "name": {**DARAZ_PRODUCT, "json_path": "name"},
        "description": {**DARAZ_PRODUCT, "json_path": "description"},
        "sub_brand": {**DARAZ_PRODUCT, "json_path": "brand.name"},
        "price": {"css": ".pdp-mod-price-v2-new-price", "pattern": r"Rs\.\s*([\d,]+(?:\.\d{1,2})?)",
                  "fallback": {**DARAZ_PRODUCT, "json_path": "offers.price", "fallback": {
                      "css": "script", "pattern": r'"pdt_price\\?":\\?"Rs\.\s*([\d,]+(?:\.\d{1,2})?)',
                  }}},
        "orignal_price": {
            "css": ".pdp-mod-price-v2-original-price", "pattern": r"Rs\.\s*([\d,]+(?:\.\d{1,2})?)",
        },
        "discount_percentage": {
            "css": ".pdp-mod-price-v2 .product-price__cut-off", "pattern": r"(\d+(?:\.\d+)?)\s*%",
        },
        "currency": {**DARAZ_PRODUCT, "json_path": "offers.priceCurrency"},
        "currency_sign": {
            "css": "script", "pattern": r'"pdt_price\\?":\\?"(Rs\.)\s*[\d,]+',
        },
        "category": {**DARAZ_PRODUCT, "json_path": "category"},
        "pictures": {
            "css": 'figure[itemprop="associatedMedia"] img[itemprop="contentUrl"][src]',
            "attr": "src",
            "fallback": {
                "css": "img.item-gallery__thumbnail-image[src]", "attr": "src",
                "fallback": {**DARAZ_PRODUCT, "json_path": "image"},
            },
        },
        "rating": {**DARAZ_PRODUCT, "json_path": "aggregateRating.ratingValue"},
        "total_rating": {**DARAZ_PRODUCT, "json_path": "aggregateRating.ratingCount"},
    },
}

AIRBNB_RENTAL = {"css": 'script[type="application/ld+json"]', "types": {"VacationRental"}}

SELECTOR_PROFILES["airbnb"] = {
    "product_links": 'a[href*="/rooms/"]',
    "structured_links": AIRBNB_RENTAL,
    "derive_brand_identity": True,
    "metadata_fields": ("query", "brand", "category", "sub_category", "source_city", "source_country"),
    "fields": {
        "name": {**AIRBNB_RENTAL, "json_path": "name",
                 "fallback": {"css": 'meta[property="og:description"]', "attr": "content"}},
        "description": {"css": 'meta[name="description"]', "attr": "content"},
        "rating": {**AIRBNB_RENTAL, "json_path": "aggregateRating.ratingValue"},
        "total_rating": {**AIRBNB_RENTAL, "json_path": "aggregateRating.ratingCount"},
        "max_guests": {**AIRBNB_RENTAL, "json_path": "containsPlace.occupancy.value"},
        "pictures": {**AIRBNB_RENTAL, "json_path": "image",
                     "fallback": {"css": 'meta[property="og:image"]', "attr": "content"}},
        "price": {**AIRBNB_RENTAL, "json_path": "offers.price"},
        "currency": {**AIRBNB_RENTAL, "json_path": "offers.priceCurrency"},
    },
}
