"""
recipe_ingester.py
------------------
Scrapes a recipe URL and saves the recipe + ingredients to your Google Sheet.

Usage:
  python recipe_ingester.py <recipe_url> <spreadsheet_id>

Example:
  python recipe_ingester.py https://www.allrecipes.com/recipe/12345/lemon-chicken \
      1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms

The script:
  1. Downloads the recipe page and extracts: title, servings, ingredients
  2. Parses each ingredient into quantity + unit + name
  3. Writes a new row to the 'Recipes' tab
  4. Writes one row per ingredient to the 'Ingredients' tab

Supported sites: AllRecipes, NYT Cooking, Serious Eats, BBC Good Food,
  Food Network, Epicurious, Bon Appétit, and ~300 more.
  For unsupported sites, wild_mode=True makes a best-effort attempt.
"""

import re
import sys
import uuid
from datetime import date

import requests
from recipe_scrapers import scrape_html
from ingredient_parser import split_and_parse
from sheets_client import get_client, get_spreadsheet

def _merge_orphan_numbers(lines: list) -> list:
    """
    Some recipe scrapers split a line like "6-8 bone-in chicken thighs" into
    two separate items: ["6-8", "bone-in chicken thighs"].  This function
    detects those orphaned number/range lines and glues them back onto the
    next line so the parser sees the complete ingredient string.
    """
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # A line is "orphaned" if it contains ONLY digits, fractions, unicode
        # fraction characters, or a numeric range — with no alphabetic content.
        is_orphan = bool(
            line
            and re.fullmatch(r"[\d½⅓⅔¼¾⅛⅜⅝⅞/.\s]+(?:[-–][\d]+)?", line)
        )
        if is_orphan and i + 1 < len(lines):
            merged.append(line + " " + lines[i + 1].strip())
            i += 2  # skip the next line — it's been merged
        else:
            merged.append(line)
            i += 1
    return merged


# Pretend to be a regular browser so sites don't block us
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def ingest_recipe(
    url: str,
    spreadsheet_id: str,
    credentials_path: str = "credentials.json",
    tags: str = "",
    cuisine: str = "",
    servings: str = "",
):
    """
    Main function. Scrapes the URL and writes to Google Sheets.
    Returns (recipe_dict, list_of_ingredient_dicts).
    """
    print(f"\n🔍 Fetching recipe from: {url}")

    # ── Fetch the page ourselves with browser-like headers, then parse ───────
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        scraper = scrape_html(response.text, org_url=url)
    except requests.HTTPError as e:
        print(f"❌ Could not fetch that URL: {e}")
        print("   The site may be blocking scrapers entirely. Try a different recipe site.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Could not scrape that URL: {e}")
        print("   Well-supported sites: Serious Eats, AllRecipes, Food Network, BBC Good Food, NYT Cooking, Epicurious")
        sys.exit(1)

    recipe_id = str(uuid.uuid4())[:8]  # Short unique ID, e.g. "a1b2c3d4"

    scraped_cuisine  = (scraper.cuisine() or "").strip()
    scraped_servings = scraper.yields() or "4"

    recipe = {
        "id":         recipe_id,
        "name":       scraper.title() or "Unknown Recipe",
        "url":        url,
        "servings":   servings.strip() if servings.strip() else scraped_servings,
        "cuisine":    cuisine.strip()  if cuisine.strip()  else scraped_cuisine,
        "tags":       tags.strip(),   # user-provided tags; scraped pages rarely supply these
        "date_added": date.today().isoformat(),
    }

    print(f"✅ Found: {recipe['name']} (serves {recipe['servings']})")

    # ── Parse each ingredient ────────────────────────────────────────────────
    raw_ingredients = scraper.ingredients()
    raw_ingredients = _merge_orphan_numbers(raw_ingredients)
    print(f"   Parsing {len(raw_ingredients)} ingredients...")

    ingredients = []
    for raw in raw_ingredients:
        for parsed in split_and_parse(raw):
            parsed["recipe_id"] = recipe_id
            parsed["recipe_name"] = recipe["name"]
            ingredients.append(parsed)

    # ── Connect to Google Sheets ─────────────────────────────────────────────
    print("   Connecting to Google Sheets...")
    try:
        gc = get_client(credentials_path)
        ss = get_spreadsheet(gc, spreadsheet_id)
    except FileNotFoundError:
        print(f"❌ credentials.json not found at '{credentials_path}'")
        print("   See setup_guide.md to create your Google API credentials.")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Could not connect to Google Sheets: {e}")
        sys.exit(1)

    # ── Write to 'Recipes' tab ───────────────────────────────────────────────
    try:
        recipes_ws = ss.worksheet("Recipes")
    except Exception:
        print("❌ Could not find a tab called 'Recipes'. Did you upload the template?")
        sys.exit(1)

    recipes_ws.append_row([
        recipe["id"],
        recipe["name"],
        recipe["url"],
        recipe["servings"],
        recipe["cuisine"],
        recipe["tags"],
        recipe["date_added"],
    ])

    # ── Write to 'Ingredients' tab ───────────────────────────────────────────
    ing_ws = ss.worksheet("Ingredients")
    rows_to_add = [
        [
            ing["recipe_id"],
            ing["recipe_name"],
            ing["ingredient"],
            ing["quantity"],
            ing["unit"],
            ing["original"],
        ]
        for ing in ingredients
    ]
    # Batch append — much faster than one row at a time
    if rows_to_add:
        ing_ws.append_rows(rows_to_add)

    print(f"✅ Saved '{recipe['name']}' → {len(ingredients)} ingredients written to Google Sheets")
    print(f"   Recipe ID: {recipe_id}")
    return recipe, ingredients


def ingest_manual(
    name: str,
    raw_ingredients_text: str,
    spreadsheet_id: str,
    url: str = "",
    servings: str = "4",
    cuisine: str = "",
    tags: str = "",
    credentials_path: str = "credentials.json",
):
    """
    Manually add a recipe by typing/pasting the ingredient list instead of
    providing a URL. raw_ingredients_text should have one ingredient per line,
    e.g.:
        2 large carrots, peeled
        1/2 cup chicken broth
        3 cloves garlic, minced
    """
    recipe_id = str(uuid.uuid4())[:8]

    recipe = {
        "id": recipe_id,
        "name": name.strip(),
        "url": url.strip(),
        "servings": servings.strip() or "4",
        "cuisine": cuisine.strip(),
        "tags": tags.strip(),
        "date_added": date.today().isoformat(),
    }

    lines = [line.strip() for line in raw_ingredients_text.splitlines() if line.strip()]
    lines = _merge_orphan_numbers(lines)
    ingredients = []
    for raw in lines:
        for parsed in split_and_parse(raw):
            parsed["recipe_id"] = recipe_id
            parsed["recipe_name"] = recipe["name"]
            ingredients.append(parsed)

    gc = get_client(credentials_path)
    ss = get_spreadsheet(gc, spreadsheet_id)

    recipes_ws = ss.worksheet("Recipes")
    recipes_ws.append_row([
        recipe["id"], recipe["name"], recipe["url"],
        recipe["servings"], recipe["cuisine"], recipe["tags"], recipe["date_added"],
    ])

    ing_ws = ss.worksheet("Ingredients")
    rows_to_add = [
        [ing["recipe_id"], ing["recipe_name"], ing["ingredient"],
         ing["quantity"], ing["unit"], ing["original"]]
        for ing in ingredients
    ]
    if rows_to_add:
        ing_ws.append_rows(rows_to_add)

    return recipe, ingredients


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python recipe_ingester.py <recipe_url> <spreadsheet_id>")
        print()
        print("Example:")
        print("  python recipe_ingester.py https://www.allrecipes.com/recipe/12345 1BxiMVs0XRA5nF...")
        sys.exit(1)

    url = sys.argv[1]
    spreadsheet_id = sys.argv[2]
    credentials = sys.argv[3] if len(sys.argv) > 3 else "credentials.json"

    ingest_recipe(url, spreadsheet_id, credentials)
