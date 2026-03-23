"""
store_scraper.py
----------------
Fetches prices from two sources and writes results to the 'Store Prices' tab
in your Google Sheet.

Sources
-------
1. Flipp (flyer deals)   — already powers the Price Tracker search tab.
                           Here we bulk-search every shopping list ingredient
                           at once and save the top deals automatically.

2. PC Express API        — the same API that powers Loblaws.ca and NoFrills.ca.
                           Returns regular (non-sale) shelf prices so you always
                           have a baseline, even when nothing is on flyer.
                           Note: if Loblaws ever changes their API, this part will
                           fail silently — Flipp results are still written.

Call refresh_store_prices(ss, postal_code) from the app to trigger a refresh.
Results land in the 'Store Prices' tab (old data is replaced each run).
"""

import uuid
import requests
from datetime import date

from flipp_client import search_flipp


# ── PC Express API (powers Loblaws.ca / NoFrills.ca) ─────────────────────────
# The API key below is embedded in the public Loblaws.ca website JavaScript —
# it is not a secret.  If Loblaws rotates the key, PC Express lookups will
# fail gracefully and Flipp results will still be saved.

PC_EXPRESS_URL = "https://api.pcexpress.ca/product-facade/v4/products/search"
PC_EXPRESS_KEY = "1im1hL52q9xvta16GlSdYDsTvxmPkNpNPLYkgzQd"

# banner → store_id pairs for a Toronto location of each chain
PC_BANNERS = {
    "Loblaws":   ("loblaws",   "1009"),
    "No Frills": ("nofrills",  "3406"),
}

STORE_PRICES_HEADERS = [
    "ingredient", "store", "product_name", "price",
    "qty_amount", "qty_unit", "price_per_unit",
    "on_sale", "sale_ends", "scraped_date", "source",
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pc_express_search(ingredient: str, banner: str, store_id: str, max_results: int = 2):
    """
    Search the PC Express product catalogue for one ingredient.
    Returns a list of dicts with keys: name, price, size.
    Returns [] on any error so a single failure doesn't stop the whole run.
    """
    headers = {
        "Accept":                    "application/json",
        "Content-Type":              "application/json",
        "x-apikey":                  PC_EXPRESS_KEY,
        "x-app-user-transfer-id":    str(uuid.uuid4()),
        "x-application-type":        "Web",
        "x-channel-id":              "Web",
        "Site-Banner":               banner,
        "x-site-context": (
            f'{{"storeId":"{store_id}","bannerId":"","businessUnitId":"","action":""}}'
        ),
    }
    body = {
        "from": 0,
        "size": max_results,
        "query": ingredient,
        "filters": {"categories": []},
    }
    try:
        r = requests.post(PC_EXPRESS_URL, json=body, headers=headers, timeout=10)
        r.raise_for_status()
        items = r.json().get("results", [])
        out = []
        for item in items:
            price = item.get("prices", {}).get("price", {}).get("value")
            name  = item.get("name", "")
            size  = item.get("packageSize", "")
            if price and name:
                out.append({"name": name, "price": float(price), "size": size})
        return out
    except Exception as exc:
        # Fail silently — PC Express is a bonus, not critical
        print(f"    [PC Express] {banner}/{ingredient}: {exc}")
        return []


def _ensure_store_prices_tab(ss):
    """Return the 'Store Prices' worksheet, creating it if it doesn't exist."""
    try:
        ws = ss.worksheet("Store Prices")
    except Exception:
        ws = ss.add_worksheet(title="Store Prices", rows=1000, cols=len(STORE_PRICES_HEADERS))
    return ws


# ── Public function called from app.py ───────────────────────────────────────

def refresh_store_prices(ss, postal_code: str = "M5V3A8"):
    """
    Reads the current 'Shopping List' tab for ingredients, then:
      1. Bulk-searches Flipp for flyer deals on every ingredient.
      2. Searches PC Express (Loblaws + No Frills) for regular shelf prices.
      3. Writes all results to the 'Store Prices' tab (full refresh).

    Returns
    -------
    rows_written : int
    errors       : list[str]   — non-fatal messages, shown to the user
    """
    errors = []

    # ── Load shopping list ingredients ────────────────────────────────────────
    try:
        shop_ws   = ss.worksheet("Shopping List")
        shop_data = shop_ws.get_all_records()
    except Exception as e:
        return 0, [f"Could not read Shopping List tab: {e}"]

    if not shop_data:
        return 0, ["Shopping list is empty — generate a meal plan first."]

    # Deduplicate while preserving order
    seen: set = set()
    ingredients = []
    for row in shop_data:
        ing = str(row.get("ingredient", "")).strip().lower()
        if ing and ing not in seen:
            ingredients.append(ing)
            seen.add(ing)

    if not ingredients:
        return 0, ["No ingredients found in your shopping list."]

    today = date.today().isoformat()
    rows: list = []

    # ── 1. Flipp bulk search ──────────────────────────────────────────────────
    print(f"[store_scraper] Searching Flipp for {len(ingredients)} ingredients…")
    for ing in ingredients:
        try:
            deals = search_flipp(ing, postal_code)
            for deal in deals[:3]:               # keep top 3 deals per ingredient
                price = deal.get("price")
                ppu   = round(float(price), 4) if price else ""
                rows.append([
                    ing,
                    deal.get("store", ""),
                    deal.get("name", ""),
                    price if price else "",
                    1,
                    "whole",
                    ppu,
                    "Yes",                        # these ARE flyer/sale prices
                    deal.get("valid_until", ""),
                    today,
                    "Flipp",
                ])
        except Exception as e:
            errors.append(f"Flipp / {ing}: {e}")

    # ── 2. PC Express (Loblaws + No Frills) ──────────────────────────────────
    print(f"[store_scraper] Checking PC Express for {len(ingredients)} ingredients…")
    for ing in ingredients:
        for store_name, (banner, store_id) in PC_BANNERS.items():
            products = _pc_express_search(ing, banner, store_id, max_results=2)
            for prod in products:
                rows.append([
                    ing,
                    store_name,
                    f"{prod['name']}" + (f" ({prod['size']})" if prod["size"] else ""),
                    prod["price"],
                    1,
                    "whole",
                    round(prod["price"], 4),      # price per unit (qty = 1 pkg)
                    "No",                          # regular shelf price, not a sale
                    "",
                    today,
                    "PC Express",
                ])

    # ── 3. Write to Store Prices tab ─────────────────────────────────────────
    try:
        ws = _ensure_store_prices_tab(ss)
        ws.clear()
        all_rows = [STORE_PRICES_HEADERS] + rows
        ws.update(f"A1", all_rows)
        print(f"[store_scraper] Wrote {len(rows)} rows to Store Prices tab.")
    except Exception as e:
        errors.append(f"Could not write Store Prices tab: {e}")
        return 0, errors

    return len(rows), errors
