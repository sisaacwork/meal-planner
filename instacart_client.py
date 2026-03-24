"""
instacart_client.py
-------------------
Searches Instacart Canada for real-time shelf prices across Toronto-area
grocery stores that don't publish their own API (Farm Boy, Longo's, Metro,
Sobeys, FreshCo, Costco, Whole Foods, T&T).

Authentication
--------------
Instacart requires a valid browser session cookie to return results.
There is no public API key — you must borrow the cookie from your own
logged-in browser session.  The cookie typically stays valid for 2–4 weeks.

How to get your cookie (one-time setup):
  1. Log in to https://www.instacart.ca in Chrome or Firefox.
  2. Open DevTools: right-click → Inspect → Network tab.
  3. Search for any grocery item on the Instacart website.
  4. In the Network tab, look for a request to "product_search" on the
     instacart.ca domain.  Click it.
  5. Under "Request Headers", find the "Cookie" line and copy its FULL value
     (it will be a long string — copy all of it).
  6. Paste it as INSTACART_COOKIE in config.py:
         INSTACART_COOKIE = "your_very_long_cookie_string_here"
     Or add it to your Streamlit secrets:
         INSTACART_COOKIE = "..."
  7. When results stop coming back, the cookie has expired — repeat step 1–6.

How to verify/update retailer slugs:
  Browse to a store on Instacart, e.g. https://www.instacart.ca/store/farm-boy
  The slug is the path segment after /store/.
"""

import requests
from price_filter import is_relevant_result

# ── Endpoint ──────────────────────────────────────────────────────────────────
_SEARCH_URL = "https://www.instacart.ca/v3/retailers/{slug}/product_search"

# ── Toronto-area retailers on Instacart Canada ────────────────────────────────
# Key   = display name used in the Store Prices sheet
# Value = retailer slug from the Instacart URL  (/store/<slug>/storefront)
#
# To add a store: visit it on instacart.ca, note the slug in the URL, add here.
INSTACART_RETAILERS = {
    "Farm Boy":    "farm-boy",
    "Longo's":     "longos",
    "Metro":       "metro-on",      # Metro Ontario
    "Sobeys":      "sobeys",
    "FreshCo":     "freshco",
    "Costco":      "costco",
    "Whole Foods": "whole-foods-market",
    "T&T":         "t-t-supermarket",
}

_HEADERS_BASE = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-CA,en-US;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Referer is set per-store in the request loop (see below)
    "X-Requested-With": "XMLHttpRequest",
}


def _parse_price(raw) -> float | None:
    """Safely convert a raw price value to float, handling '$2.99' strings."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def search_instacart(
    ingredient: str,
    cookie: str,
    stores: dict | None = None,
    max_per_store: int = 2,
) -> list:
    """
    Search Instacart Canada for an ingredient across the configured stores.

    Parameters
    ----------
    ingredient    : the ingredient name to search for
    cookie        : full value of the Cookie header from a logged-in browser session
    stores        : override INSTACART_RETAILERS with a custom {name: slug} dict
    max_per_store : maximum results to keep per store (default 2)

    Returns
    -------
    list of dicts, each with keys: store, name, price, size
    Returns [] if cookie is empty, expired, or all stores fail.
    """
    if not cookie or not cookie.strip():
        return []

    retailer_map = stores if stores is not None else INSTACART_RETAILERS
    results = []
    cookie_expired = False

    for store_name, slug in retailer_map.items():
        if cookie_expired:
            break  # no point hammering the API once we know auth is dead

        url = _SEARCH_URL.format(slug=slug)

        # Instacart's internal API uses "term" as the search query key.
        # "source" and "per_page" are standard pagination params.
        params = {
            "term":     ingredient,
            "source":   "search",
            "per_page": 20,   # fetch more so relevance filter has material to work with
            "page":     1,
        }

        # Use a per-store Referer so the request looks like a real browser
        headers = {
            **_HEADERS_BASE,
            "Cookie":  cookie.strip(),
            "Referer": f"https://www.instacart.ca/store/{slug}/storefront",
        }

        try:
            r = requests.get(url, params=params, headers=headers, timeout=12)

            # Auth failures mean the cookie has expired for ALL stores
            if r.status_code in (401, 403):
                print(
                    f"    [Instacart] Auth error ({r.status_code}) — "
                    "cookie may be expired.  See instacart_client.py for refresh steps."
                )
                cookie_expired = True
                break

            # 404 / 422 typically means the retailer slug is wrong or unavailable
            if r.status_code == 404 or r.status_code == 422:
                print(f"    [Instacart] {store_name}: store not found (slug={slug!r})")
                continue

            if not r.ok:
                print(f"    [Instacart] {store_name}: HTTP {r.status_code}")
                continue

            data = r.json()

            # Handle both flat {"products": [...]} and nested {"data": {"products": [...]}}
            products = (
                data.get("products")
                or data.get("items")
                or (data.get("data") or {}).get("products")
                or []
            )

            count = 0
            for prod in products:
                if count >= max_per_store:
                    break

                # Products may be wrapped in an extra "item" key
                if isinstance(prod, dict) and "item" in prod:
                    prod = prod["item"]

                name  = (prod.get("name") or prod.get("display_name") or "").strip()
                price = _parse_price(
                    prod.get("price")
                    or prod.get("current_price")
                    or prod.get("display_price")
                )
                size  = (
                    prod.get("size")
                    or prod.get("unit_size")
                    or prod.get("package_size")
                    or ""
                ).strip()

                if not name or price is None:
                    continue

                # Apply the same relevance filter used by the Flipp client
                if not is_relevant_result(ingredient, name):
                    continue

                results.append({
                    "store": store_name,
                    "name":  name,
                    "price": price,
                    "size":  size,
                })
                count += 1

        except Exception as exc:
            print(f"    [Instacart] {store_name}/{ingredient}: {exc}")

    return results


def cookie_looks_configured(cookie: str | None) -> bool:
    """Quick sanity check — is the cookie set to something real, not the placeholder?"""
    if not cookie:
        return False
    placeholder_phrases = ("your_cookie", "paste_here", "replace_me", "instacart_session=")
    return len(cookie.strip()) > 50 and not any(p in cookie.lower() for p in placeholder_phrases)
