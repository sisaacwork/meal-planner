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

import re
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

# ── Relevance filtering ───────────────────────────────────────────────────────

# Words that appear in a product name and signal it's a *processed form* of the
# raw ingredient rather than the ingredient itself.  We filter out products that
# contain one of these words UNLESS the search term itself contains that word
# (e.g. searching "tomato sauce" should still return tomato sauce products).
_PROCESSED_FORMS = {
    "pesto", "sauce", "paste", "spread", "jam", "jelly", "extract",
    "ketchup", "salsa", "dip", "powder", "flakes", "seasoning",
    "blend", "mix", "marinade", "syrup", "concentrate",
    "puree", "purée", "relish", "chutney", "coulis",
    "soup", "stew", "curry", "gravy",
    "pickle", "pickled", "fermented",
    "flavour", "flavor", "infused",
    "oil",       # basil oil, garlic oil etc — NOT a raw ingredient
    "vinegar",   # e.g. "balsamic vinegar" already in search term → won't filter
    "butter",    # e.g. "peanut butter" already in search term
    "cream",     # e.g. "sour cream" already in search term
    "juice",     # e.g. "lemon juice" already in search term
}

# Words we strip when extracting "key words" from the ingredient name so that
# descriptors like "fresh" or "extra" don't disqualify good results.
_ING_STOP_WORDS = {
    "fresh", "dried", "frozen", "canned", "whole", "ground",
    "chopped", "sliced", "diced", "minced", "cooked", "raw",
    "organic", "extra", "virgin", "large", "small", "medium",
    "boneless", "skinless", "free", "range",
    "a", "an", "the", "of", "and", "or", "for", "with", "to",
}


def _key_words(text: str) -> list:
    """Return meaningful lowercase tokens from an ingredient or product string."""
    return [
        w.lower().strip(".,;:-()[]\"'")
        for w in text.split()
        if w.lower().strip(".,;:-()[]\"'") not in _ING_STOP_WORDS
        and len(w.strip(".,;:-()[]\"'")) > 1
        # Skip size/weight tokens like "180ml", "1L", "35g"
        and not re.fullmatch(r"[\d.,]+(?:g|kg|ml|l|oz|lb|pk|ct)?", w.lower().strip(".,;:-()[]"))
    ]


def _word_in_text(needle: str, text: str) -> bool:
    """
    Check whether 'needle' (or a close plural/singular form) appears as a
    complete word inside 'text'.  Case-insensitive.
    """
    text = text.lower()
    # Build a small set of forms to try: exact, singular (strip 's'), plural (+s)
    stem = needle.rstrip("s") if len(needle) > 3 else needle
    for form in {needle, stem, stem + "s", stem + "es"}:
        if re.search(r"\b" + re.escape(form) + r"\b", text):
            return True
    return False


def _is_relevant_result(ingredient: str, product_name: str) -> bool:
    """
    Return True only if the Flipp product is a plausible match for the
    ingredient being searched.

    Two rules must both pass:

    Rule 1 — Whole-word match
        Every key word from the ingredient name must appear as a whole word in
        the product name.  This stops "black pepper" matching "green peppers"
        (the word "black" is absent) and "basil" matching "basil pesto" only
        superficially (actually pesto passes rule 1 — rule 2 catches it).

    Rule 2 — No unmatched processed-form indicator
        If the product name contains a word from _PROCESSED_FORMS that is NOT
        present in the ingredient name, the product is a processed derivative
        (e.g. "basil pesto", "garlic powder") and is filtered out.
    """
    ing_words = _key_words(ingredient)
    if not ing_words:
        return True  # can't judge — let it through

    # Rule 1: every ingredient keyword must appear in the product name
    for word in ing_words:
        if not _word_in_text(word, product_name):
            return False

    # Rule 2: reject products whose name implies a processed form of the ingredient
    # (unless the ingredient name itself contains that processing word)
    ing_lower = ingredient.lower()
    product_lower = product_name.lower()
    for form in _PROCESSED_FORMS:
        form_in_product = re.search(r"\b" + re.escape(form) + r"\b", product_lower)
        form_in_ingredient = re.search(r"\b" + re.escape(form) + r"\b", ing_lower)
        if form_in_product and not form_in_ingredient:
            return False

    return True


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

        product_name = item.get("name") or item.get("display_name") or ingredient

        # ── Relevance filter ─────────────────────────────────────────────────
        # Skip products that don't genuinely match the ingredient being searched.
        # e.g. searching "basil" should not return "Farm Boy Basil Pesto 180ml",
        #      and "black pepper" should not return "Bulk green peppers".
        if not _is_relevant_result(ingredient, product_name):
            continue

        store = (
            item.get("merchant_name")
            or item.get("merchant")
            or item.get("retailer_name")
            or "Unknown store"
        )

        results.append({
            "name": product_name,
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
