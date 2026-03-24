"""
store_scraper.py
----------------
Fetches prices from three sources and writes results to the 'Store Prices' tab
in your Google Sheet.

Sources (in priority order)
----------------------------
1. Instacart          — real-time shelf prices from Farm Boy, Longo's, Metro,
                         Sobeys, FreshCo, Costco, Whole Foods, T&T, and more.
                         Requires a session cookie (see instacart_client.py).
                         Best source: covers the stores that don't have their own
                         public API.

2. Flipp              — weekly flyer deals aggregated from most major chains.
                         No authentication needed.  Great for catching sales you
                         might otherwise miss.

3. PC Express         — regular (non-sale) shelf prices for Loblaws and No Frills
                         via the same API that powers their websites.
                         Requires no authentication but the API key embedded in
                         their JS may rotate; fails gracefully if it does.

Call refresh_store_prices(ss, postal_code, instacart_cookie) from app.py.
Results land in the 'Store Prices' tab (full refresh each run).
"""

import uuid
import requests
from datetime import date

from flipp_client import search_flipp
from instacart_client import search_instacart, cookie_looks_configured


# ── PC Express API (powers Loblaws.ca / NoFrills.ca) ─────────────────────────
PC_EXPRESS_URL = "https://api.pcexpress.ca/product-facade/v4/products/search"
PC_EXPRESS_KEY = "1im1hL52q9xvta16GlSdYDsTvxmPkNpNPLYkgzQd"

# banner → store_id pairs for a Toronto location of each chain
PC_BANNERS = {
    "Loblaws":   ("loblaws",  "1009"),
    "No Frills": ("nofrills", "3406"),
}

STORE_PRICES_HEADERS = [
    "ingredient", "store", "product_name", "price",
    "qty_amount", "qty_unit", "price_per_unit",
    "on_sale", "sale_ends", "scraped_date", "source",
]


# ── PC Express helper ─────────────────────────────────────────────────────────

def _pc_express_search(ingredient: str, banner: str, store_id: str, max_results: int = 2):
    """
    Search the PC Express product catalogue for one ingredient.
    Returns a list of dicts with keys: name, price, size.
    Returns [] on any error so a single failure doesn't stop the whole run.
    """
    headers = {
        "Accept":                 "application/json, text/plain, */*",
        "Content-Type":           "application/json",
        "x-apikey":               PC_EXPRESS_KEY,
        "x-app-user-transfer-id": str(uuid.uuid4()),
        "x-application-type":     "Web",
        "x-channel-id":           "Web",
        "Site-Banner":            banner,
        "x-site-context": (
            f'{{"storeId":"{store_id}","bannerId":"","businessUnitId":"","action":""}}'
        ),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Origin":  "https://www.loblaws.ca",
        "Referer": "https://www.loblaws.ca/",
    }
    body = {
        "from":    0,
        "size":    max_results,
        "query":   ingredient,
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
        print(f"    [PC Express] {banner}/{ingredient}: {exc}")
        return []


# ── Sheet helper ──────────────────────────────────────────────────────────────

def _ensure_store_prices_tab(ss):
    """Return the 'Store Prices' worksheet, creating it if it doesn't exist."""
    try:
        ws = ss.worksheet("Store Prices")
    except Exception:
        ws = ss.add_worksheet(title="Store Prices", rows=1000, cols=len(STORE_PRICES_HEADERS))
    return ws


# ── Public entry point ────────────────────────────────────────────────────────

def refresh_store_prices(
    ss,
    postal_code: str = "M5V3A8",
    instacart_cookie: str = "",
):
    """
    Read the current Shopping List tab for ingredients, then search all three
    price sources in priority order and write results to the Store Prices tab.

    Parameters
    ----------
    ss                 : gspread Spreadsheet object
    postal_code        : used by Flipp to find local flyer deals
    instacart_cookie   : browser session cookie for Instacart (optional but recommended)

    Returns
    -------
    (rows_written, errors, source_counts)
      rows_written  : total rows saved
      errors        : list of non-fatal warning strings to show the user
      source_counts : dict of {source_name: row_count} for the summary banner
    """
    errors = []
    source_counts: dict = {"Instacart": 0, "Flipp": 0, "PC Express": 0}

    # ── Load shopping list ingredients ────────────────────────────────────────
    try:
        shop_ws   = ss.worksheet("Shopping List")
        shop_data = shop_ws.get_all_records()
    except Exception as e:
        return 0, [f"Could not read Shopping List tab: {e}"], source_counts

    if not shop_data:
        return 0, ["Shopping list is empty — generate a meal plan first."], source_counts

    # Deduplicate while preserving order
    seen: set = set()
    ingredients = []
    for row in shop_data:
        ing = str(row.get("ingredient", "")).strip().lower()
        if ing and ing not in seen:
            ingredients.append(ing)
            seen.add(ing)

    if not ingredients:
        return 0, ["No ingredients found in your shopping list."], source_counts

    today = date.today().isoformat()
    rows: list = []

    # ── Source 1: Instacart ───────────────────────────────────────────────────
    use_instacart = cookie_looks_configured(instacart_cookie)
    if use_instacart:
        print(f"[store_scraper] Searching Instacart for {len(ingredients)} ingredients…")
        for ing in ingredients:
            products = search_instacart(ing, instacart_cookie, max_per_store=2)
            for prod in products:
                price = prod["price"]
                rows.append([
                    ing,
                    prod["store"],
                    prod["name"] + (f" ({prod['size']})" if prod.get("size") else ""),
                    price,
                    1,
                    "whole",
                    round(price, 4),
                    "No",    # real-time shelf price, not necessarily a sale
                    "",
                    today,
                    "Instacart",
                ])
                source_counts["Instacart"] += 1
    else:
        print("[store_scraper] Instacart cookie not configured — skipping.")
        errors.append(
            "Instacart not searched: no cookie configured.  "
            "See the setup instructions in the Auto-fetch tab to add one."
        )

    # ── Source 2: Flipp ───────────────────────────────────────────────────────
    print(f"[store_scraper] Searching Flipp for {len(ingredients)} ingredients…")
    for ing in ingredients:
        try:
            deals = search_flipp(ing, postal_code)
            for deal in deals[:3]:          # keep top 3 deals per ingredient
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
                    "Yes",                  # Flipp results are always flyer/sale prices
                    deal.get("valid_until", ""),
                    today,
                    "Flipp",
                ])
                source_counts["Flipp"] += 1
        except Exception as e:
            errors.append(f"Flipp / {ing}: {e}")

    # ── Source 3: PC Express (Loblaws + No Frills) ────────────────────────────
    print(f"[store_scraper] Checking PC Express for {len(ingredients)} ingredients…")
    for ing in ingredients:
        for store_name, (banner, store_id) in PC_BANNERS.items():
            products = _pc_express_search(ing, banner, store_id, max_results=2)
            for prod in products:
                rows.append([
                    ing,
                    store_name,
                    prod["name"] + (f" ({prod['size']})" if prod.get("size") else ""),
                    prod["price"],
                    1,
                    "whole",
                    round(prod["price"], 4),
                    "No",
                    "",
                    today,
                    "PC Express",
                ])
                source_counts["PC Express"] += 1

    # ── Write to Store Prices tab ─────────────────────────────────────────────
    try:
        ws = _ensure_store_prices_tab(ss)
        ws.clear()
        ws.update("A1", [STORE_PRICES_HEADERS] + rows)
        print(f"[store_scraper] Wrote {len(rows)} rows to Store Prices tab.")
    except Exception as e:
        errors.append(f"Could not write Store Prices tab: {e}")
        return 0, errors, source_counts

    return len(rows), errors, source_counts
