"""Website targets and their listing sources."""

# TARGET = KHAADI_TARGET
KHAADI_TARGET = {
    "url": "https://pk.khaadi.com/ready-to-wear/chapter-2/kurta-/",
    "brand": "khaadi", "selector_profile": "khaadi",
    "product_id_pattern": r"/([^/]+)\.html$",
    "sources": [{"q": "Chapter 2 Kurtas", "gender": "women",
                 "l_category": "ready to wear", "l_subcategory": "kurta"}],
}
BREAKOUT_TARGET = {
    "url": "https://www.breakout.com.pk/collections/men-jacket",
    "brand": "breakout", "selector_profile": "breakout",
    "product_id_pattern": r"/products/([^/]+)$",
    "sources": [{"q": "Jackets / Coats", "gender": "men",
                 "l_category": "clothing", "l_subcategory": "jackets"}],
}
ALEXANDER_MCQUEEN_TARGET = {
    "url": "https://www.alexandermcqueen.com/en-us/ca/women/shoes",
    "brand": "alexandermcqueen", "selector_profile": "alexandermcqueen",
    "product_id_pattern": r"-([A-Za-z0-9]+)\.html$",
    "sources": [{"q": "Shoes", "gender": "women", "l_category": "shoes"}],
}
LAMA_TARGET = {
    "url": "https://pk.lamaretail.com/collections/woman-boots",
    "brand": "lama", "selector_profile": "lama",
    "product_id_pattern": r"/products/([^/]+)$",
    "sources": [{"q": "Boots", "gender": "women",
                 "l_category": "shoes", "l_subcategory": "boots"}],
}

OLX_TARGET = {
    "url": "https://www.olx.com.pk",
    "brand": "olx", "selector_profile": "olx",
    "product_id_pattern": r"-iid-(\d+)/?$",
    "sources": [
        {"q": "mobiles", "path": "/mobiles_c1411", "l_category": "mobiles"},
        {"q": "vehicles", "path": "/vehicles_c5", "l_category": "vehicles"},
        {"q": "Lahore mobiles", "path": "/lahore_g4060673/mobiles_c1411",
         "city": "Lahore", "l_category": "mobiles"},
        {"q": "iphone", "path": "/lahore_g4060673/mobiles_c1411/q-iphone",
         "city": "Lahore", "l_category": "mobiles"},
    ],
}

DARAZ_TARGET = {
    "url": "https://www.daraz.pk/chocolates/",
    "brand": "daraz", "selector_profile": "daraz",
    "max_pages": None,
    "query_param": "q",
    "product_id_pattern": r"-i(\d+)(?:-s\d+)?\.html$",
    "sources": [
        {"q": query, "params": {"ajax": "true"}, "l_category": "chocolates"}
        for query in ("kellogs coco pops", "ferrero rocher", "cadbury dairy milk",
                      "kitkat", "lindt chocolate")
    ],
}

AIRBNB_CITIES = {
    "Pakistan": {"Islamabad": "islamabad-pakistan", "Lahore": "lahore-pakistan",
                 "Karachi": "karachi-pakistan", "Rawalpindi": "rawalpindi-pakistan",
                 "Murree": "murree-pakistan"},
    "United Arab Emirates": {"Dubai": "dubai-united-arab-emirates",
                             "Abu Dhabi": "abu-dhabi-united-arab-emirates",
                             "Sharjah": "sharjah-united-arab-emirates",
                             "Ajman": "ajman-united-arab-emirates",
                             "Ras Al Khaimah": "ras-al-khaimah-united-arab-emirates"},
    "United Kingdom": {"London": "london-united-kingdom", "Manchester": "manchester-united-kingdom",
                       "Edinburgh": "edinburgh-united-kingdom", "Birmingham": "birmingham-united-kingdom",
                       "Liverpool": "liverpool-united-kingdom"},
    "Türkiye": {"Istanbul": "istanbul-turkiye", "Antalya": "antalya-turkiye", "Izmir": "izmir-turkiye",
                "Bursa": "bursa-turkiye", "Bodrum": "bodrum-turkiye"},
    "Canada": {"Toronto": "toronto-canada", "Vancouver": "vancouver-canada", "Montreal": "montreal-canada",
               "Calgary": "calgary-canada", "Ottawa": "ottawa-canada"},
}

AIRBNB_TARGET = {
    "url": "https://www.airbnb.com/islamabad-pakistan/stays",
    "brand": "airbnb", "selector_profile": "airbnb",
    "product_id_pattern": r"/rooms/(\d+)",
    "detail_params": {"adults": 2, "currency": "USD"},
    "sources": [
        {"q": city, "city": city, "country": country, "path": f"/{slug}/stays",
         "l_category": "stays", "l_subcategory": "all stays"}
        for country, cities in AIRBNB_CITIES.items()
        for city, slug in cities.items()
    ] + [
        {"q": "Islamabad", "city": "Islamabad", "country": "Pakistan",
         "path": f"/islamabad-pakistan/stays/{kind.replace(' ', '-')}",
         "l_category": "stays", "l_subcategory": kind}
        for kind in ("apartments", "houses", "villas", "hotels", "private suites")
    ],
}

VINTED_TARGET = {
    "url": "https://www.vinted.co.uk/catalog/1231-shoes",
    "brand": "vinted", "selector_profile": "vinted",
    "query_param": "search_text",
    "product_id_pattern": r"/items/(\d+)(?:-|/|$)",
    "sources": [
        {"q": "boots", "path": "/catalog/1233-men_boots", "gender": "men",
         "l_category": "shoes", "l_subcategory": "boots"},
        {"q": "trainers", "path": "/catalog/1242-men_sneakers", "gender": "men",
         "l_category": "shoes", "l_subcategory": "trainers"},
    ],
}

# TARGET = OLX_TARGET
# TARGET = DARAZ_TARGET
TARGET = AIRBNB_TARGET
# TARGET = VINTED_TARGET
# TARGET = LAMA_TARGET
# TARGET = KHAADI_TARGET
# TARGET = BREAKOUT_TARGET
# TARGET = ALEXANDER_MCQUEEN_TARGET
MAX_PAGES = TARGET.get("max_pages", 1)
