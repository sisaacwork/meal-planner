"""
flipp_client.py
---------------
Searches Flipp for current Canadian grocery flyer deals.
Flipp aggregates weekly flyers from Loblaws, No Frills, Metro, Farm Boy,
Longo's, Sobeys, Walmart, FreshCo, and many more.

This uses Flipp's unofficial (reverse-engineered) API — it works reliably
but could change without notice. If it stops working, the app falls back
to showing a direct Flipp search link instead.
"""

import requests

FLIPP_SEARCH_URL = "https://backflipp.wishabi.com/flipp/items/search"

# Toronto-area stores we care about (used to filter Flipp results)
TORONTO_STORES = [
    "Loblaws",
    "No Frills",
    "Metro",
    "Farm Boy",
    "Longo's",
    "Sobeys",
    "Costco",
    "Walmart",
    "FreshCo",
    "Whole Foods Market",
    "T&T Supermarket",
    "Nations Fresh Foods",
    "Summerhill Market",
    "Fiesta Farms",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://flipp.com/",
}


def search_flipp(ingredient: str, postal_code: str = "M5V3A8") -> list[dict]:
    """
    Search Flipp for current flyer deals on a given ingredient.

    Returns a list of deal dicts, each with:
      - name          : product name as shown in the flyer
      - store         : retailer name
      - price         : current sale price (float or None)
      - price_text    : price as formatted string, e.g. "$1.99" or "2 for $5"
      - unit          : unit/size description from the flyer
      - valid_until   : sale end date string
      - flipp_url     : direct link to the item on Flipp
      - image_url     : product image URL (may be empty)

    Returns an empty list if the API is unreachable or returns no results.
    """
    params = {
        "locale": "en-CA",
        "postal_code": postal_code.replace(" ", ""),
        "q": ingredient,
    }

    try:
        resp = requests.get(FLIPP_SEARCH_URL, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    # Flipp returns either a list directly or a dict with an "items" key
    items = data if isinstance(data, list) else data.get("items", [])

    results = []
    for item in items:
        # Skip items with no price info
        current_price = item.get("current_price") or item.get("price")
        price_text = item.get("display_type") or item.get("sale_story") or ""
        if current_price:
            price_text = f"${float(current_price):.2f}"

        if not price_text:
            continue

        store = (
            item.get("merchant_name")
            or item.get("merchant")
            or item.get("retailer_name")
            or "Unknown store"
        )

        results.append({
            "name": item.get("name") or item.get("display_name") or ingredient,
            "store": store,
            "price": float(current_price) if current_price else None,
            "price_text": price_text,
            "unit": item.get("description") or item.get("unit_of_measure") or "",
            "valid_until": item.get("valid_to") or item.get("end_date") or "",
            "flipp_url": f"https://flipp.com/en-ca/flyers/items/{item.get('id', '')}",
            "image_url": item.get("cutout_image_url") or item.get("image_url") or "",
        })

    # Sort: items on sale at known Toronto stores first, then by price
    def sort_key(deal):
        known = any(s.lower() in deal["store"].lower() for s in TORONTO_STORES)
        price = deal["price"] or 999
        return (not known, price)

    return sorted(results, key=sort_key)


def flipp_web_search_url(ingredient: str, postal_code: str = "M5V3A8") -> str:
    """Returns a direct Flipp.com URL for manual searching as a fallback."""
    query = ingredient.replace(" ", "+")
    return f"https://flipp.com/en-ca/flyers?query={query}&postal_code={postal_code}"
