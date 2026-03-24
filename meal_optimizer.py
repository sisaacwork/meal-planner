"""
meal_optimizer.py
-----------------
Reads your saved recipes from Google Sheets and suggests a weekly meal plan
that maximizes ingredient overlap — so you buy bigger quantities of fewer
ingredients and waste as little as possible.

Usage:
  python meal_optimizer.py <spreadsheet_id> [num_meals] [--write]

  num_meals : how many dinners to plan (default: 7)
  --write   : if present, writes the plan back to the 'Meal Plan' and
              'Shopping List' tabs in your Google Sheet

Example:
  python meal_optimizer.py 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms 7 --write

How the optimizer works:
  1. Load all recipes and their ingredients from your sheet
  2. Load current prices from the Price Tracker tab
  3. Use a greedy algorithm to pick recipes one at a time, always choosing
     the next recipe that maximizes shared ingredients with already-chosen ones
  4. Consolidate the shopping list and flag the cheapest store per item
"""

import sys
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd

from sheets_client import get_client, get_spreadsheet
from unit_converter import normalise_to_base, same_dimension, convert_to_metric


DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Data loading ─────────────────────────────────────────────────────────────

def load_data(ss):
    """Load recipes, ingredients, and prices from Google Sheets into DataFrames."""
    try:
        recipes_df = pd.DataFrame(ss.worksheet("Recipes").get_all_records())
        ing_df = pd.DataFrame(ss.worksheet("Ingredients").get_all_records())
        prices_df = pd.DataFrame(ss.worksheet("Price Tracker").get_all_records())
    except Exception as e:
        print(f"❌ Error reading from Google Sheets: {e}")
        sys.exit(1)

    if recipes_df.empty:
        print("❌ No recipes found. Add some first with recipe_ingester.py")
        sys.exit(1)

    # Normalize column names (lowercase, strip spaces)
    # Use a list comprehension instead of .str accessor — gspread sometimes
    # returns integer column indices (when the sheet is empty or has no header)
    # which cause "Can only use .str accessor with string values" errors.
    recipes_df.columns = [str(c).lower().strip() for c in recipes_df.columns]
    ing_df.columns     = [str(c).lower().strip() for c in ing_df.columns]
    prices_df.columns  = [str(c).lower().strip() for c in prices_df.columns]

    return recipes_df, ing_df, prices_df


def build_ingredient_map(ing_df):
    """
    Build a dict: recipe_id → { ingredient_name → {quantity, unit} }
    This is what the optimizer looks at when scoring overlap.
    """
    ing_map = defaultdict(dict)
    for _, row in ing_df.iterrows():
        rid = str(row.get("recipe_id", "")).strip()
        name = str(row.get("ingredient", "")).strip().lower()
        try:
            qty = float(row.get("quantity", 1))
        except (ValueError, TypeError):
            qty = 1.0
        unit = str(row.get("unit", "whole")).strip()
        if rid and name:
            ing_map[rid][name] = {"quantity": qty, "unit": unit}
    return ing_map


def build_price_map(prices_df):
    """
    Build a dict: ingredient_name → best (cheapest) store info.
    If an item is on sale, that takes priority.
    Returns: { ingredient: { store, price_per_unit, unit, on_sale } }
    """
    best = {}
    if prices_df.empty:
        return best

    for _, row in prices_df.iterrows():
        ingredient = str(row.get("ingredient", "")).strip().lower()
        store = str(row.get("store", "")).strip()
        on_sale = str(row.get("on_sale", "")).strip().lower() in ("yes", "true", "1")

        try:
            ppu = float(str(row.get("price_per_unit", "0")).replace("$", "").strip())
        except (ValueError, TypeError):
            continue

        if not ingredient or not store or ppu <= 0:
            continue

        if ingredient not in best:
            best[ingredient] = {"store": store, "price_per_unit": ppu,
                                "unit": row.get("unit", ""), "on_sale": on_sale}
        else:
            existing = best[ingredient]
            # Prefer sales; within same sale status, prefer lower price
            if (on_sale and not existing["on_sale"]) or \
               (on_sale == existing["on_sale"] and ppu < existing["price_per_unit"]):
                best[ingredient] = {"store": store, "price_per_unit": ppu,
                                    "unit": row.get("unit", ""), "on_sale": on_sale}
    return best


# ── Optimization logic ────────────────────────────────────────────────────────

def overlap_score(selected_ids, candidate_id, ing_map):
    """
    Score how much a candidate recipe overlaps with already-selected recipes.
    Returns a value between 0.0 and 1.0.
    Higher = more shared ingredients = less waste.
    """
    all_ids = selected_ids + [candidate_id]
    ingredient_counts = defaultdict(int)
    for rid in all_ids:
        for ing in ing_map.get(rid, {}):
            ingredient_counts[ing] += 1

    total = len(ingredient_counts)
    if total == 0:
        return 0.0

    shared = sum(1 for count in ingredient_counts.values() if count > 1)
    return shared / total


def greedy_meal_plan(recipe_ids, ing_map, num_meals, ratings_map=None):
    """
    Greedy algorithm: pick recipes one at a time.
    Each pick chooses whichever recipe maximizes ingredient overlap
    with the ones already chosen.

    ratings_map (optional): dict of recipe_id → "like" | "dislike"
      - Disliked recipes are excluded from the pool entirely.
      - Liked recipes get a +0.2 score bonus so they're preferred when close.
    """
    # Filter out recipes you've marked as disliked
    if ratings_map:
        recipe_ids = [r for r in recipe_ids if ratings_map.get(r) != "dislike"]

    num_meals = min(num_meals, max(1, len(recipe_ids)))
    remaining = list(recipe_ids)

    if not remaining:
        return []

    # Seed: start with the recipe that has the most ingredients
    # (more ingredients = more potential for overlap with others)
    first = max(remaining, key=lambda r: len(ing_map.get(r, {})))
    selected = [first]
    remaining.remove(first)

    while len(selected) < num_meals and remaining:
        best_score = -1.0
        best_recipe = None
        for candidate in remaining:
            score = overlap_score(selected, candidate, ing_map)
            # Liked recipes get a small boost so they're preferred when scores are close
            if ratings_map and ratings_map.get(candidate) == "like":
                score = min(1.0, score + 0.2)
            if score > best_score:
                best_score = score
                best_recipe = candidate
        selected.append(best_recipe)
        remaining.remove(best_recipe)

    return selected


# ── Output formatting ─────────────────────────────────────────────────────────

def build_shopping_list(selected_ids, ing_map, price_map, pantry_map=None):
    """
    Consolidate all ingredients across selected recipes.

    Unit normalisation: if the same ingredient appears with different but
    compatible units (e.g. "cup" and "ml"), they are first converted to a
    common base (ml or g) and summed, then smart-formatted back to metric.
    Incompatible units (e.g. "whole" vs "g") are kept as separate rows.

    pantry_map (optional): dict of ingredient → {quantity, unit} for items
      you already have at home.
      - If pantry covers the full needed quantity → in_pantry="Yes", item
        is pre-ticked on the shopping list.
      - If pantry covers part of it → quantity is reduced to the remainder,
        and a "🧺 Partial (have X unit)" note is added.
      - If pantry has no quantity info → in_pantry="Yes" (assume covered).
    """
    # ingredient → base_unit ("ml"/"g"/original) → total base quantity
    base_totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for rid in selected_ids:
        for ingredient, details in ing_map.get(rid, {}).items():
            qty  = details["quantity"]
            unit = details["unit"]
            base = normalise_to_base(qty, unit)
            if base:
                base_qty, base_unit = base
                base_totals[ingredient][base_unit] += base_qty
            else:
                # Count unit or unknown — bucket by the unit itself
                base_totals[ingredient][unit] += qty

    shopping = []
    for ingredient, buckets in sorted(base_totals.items()):
        for base_unit, total_base_qty in buckets.items():

            # ── Pantry adjustment (before display formatting) ─────────────
            in_pantry   = "No"
            pantry_note = ""
            adj_qty     = total_base_qty   # may be reduced by what's in stock

            if pantry_map and ingredient in pantry_map:
                p_info  = pantry_map[ingredient]
                p_qty   = p_info.get("quantity")
                p_unit  = str(p_info.get("unit", "")).strip()

                if p_qty and p_unit:
                    have_base = normalise_to_base(float(p_qty), p_unit)
                    need_base = normalise_to_base(total_base_qty, base_unit)

                    if have_base and need_base and same_dimension(have_base[1], need_base[1]):
                        # Both are volume or both are weight → compare directly
                        have_qty_base = have_base[0]
                        need_qty_base = need_base[0]
                        if have_qty_base >= need_qty_base:
                            in_pantry   = "Yes"
                            pantry_note = "🧺 In pantry"
                        else:
                            # Need to buy the shortfall
                            diff = need_qty_base - have_qty_base
                            adj_qty = diff             # still in the base unit (ml or g)
                            h_disp, h_unit = convert_to_metric(have_qty_base, have_base[1])
                            pantry_note = f"🧺 Partial (have {h_disp} {h_unit})"

                    elif not have_base and not need_base:
                        # Both are count/whole units — compare if units match
                        if p_unit.lower() == base_unit.lower():
                            if float(p_qty) >= total_base_qty:
                                in_pantry   = "Yes"
                                pantry_note = "🧺 In pantry"
                            else:
                                adj_qty = total_base_qty - float(p_qty)
                                pantry_note = f"🧺 Partial (have {p_qty} {p_unit})"
                        else:
                            # Different count units (e.g. "clove" vs "whole") — assume covered
                            in_pantry   = "Yes"
                            pantry_note = "🧺 In pantry"
                    else:
                        # Volume vs weight mismatch — can't subtract, assume covered
                        in_pantry   = "Yes"
                        pantry_note = "🧺 In pantry"
                else:
                    # No quantity recorded — assume they have enough
                    in_pantry   = "Yes"
                    pantry_note = "🧺 In pantry"

            # Smart-format the (possibly reduced) quantity
            display_qty, display_unit = convert_to_metric(adj_qty, base_unit)

            price_info     = price_map.get(ingredient, {})
            estimated_cost = ""
            if price_info.get("price_per_unit") and in_pantry != "Yes":
                estimated_cost = round(display_qty * price_info["price_per_unit"], 2)

            notes_parts = [pantry_note] if pantry_note else []
            if price_info.get("on_sale"):
                notes_parts.append("ON SALE")

            shopping.append({
                "ingredient":     ingredient,
                "total_quantity": display_qty,
                "unit":           display_unit,
                "best_store":     price_info.get("store", "—"),
                "unit_price":     price_info.get("price_per_unit", ""),
                "estimated_cost": estimated_cost,
                "in_pantry":      in_pantry,
                "notes":          "  ".join(notes_parts),
            })
    return shopping


def print_plan(selected_ids, recipe_names, ing_map, price_map):
    """Pretty-print the meal plan and shopping list to the terminal."""
    print("\n" + "═" * 60)
    print("  WEEKLY MEAL PLAN")
    print("═" * 60)
    for i, rid in enumerate(selected_ids):
        day = DAYS_OF_WEEK[i] if i < 7 else f"Day {i+1}"
        print(f"  {day:<12} {recipe_names.get(rid, rid)}")

    # Ingredient overlap stats
    ingredient_counts = defaultdict(list)
    for rid in selected_ids:
        for ing in ing_map.get(rid, {}):
            ingredient_counts[ing].append(recipe_names.get(rid, rid))

    shared = {ing: recipes for ing, recipes in ingredient_counts.items() if len(recipes) > 1}
    score = len(shared) / len(ingredient_counts) if ingredient_counts else 0

    print(f"\n  Ingredient overlap: {score:.0%}  ({len(shared)} of {len(ingredient_counts)} ingredients shared)")

    if shared:
        print("\n  Shared ingredients (buy in bulk):")
        for ing, recipes in sorted(shared.items()):
            recipe_list = ", ".join(recipes)
            print(f"    • {ing}: {recipe_list}")

    shopping = build_shopping_list(selected_ids, ing_map, price_map)
    total_cost = sum(item["estimated_cost"] for item in shopping if isinstance(item["estimated_cost"], float))

    print(f"\n{'═' * 60}")
    print(f"  SHOPPING LIST  ({len(shopping)} items)")
    if total_cost:
        print(f"  Estimated total: ${total_cost:.2f} CAD")
    print("═" * 60)
    print(f"  {'INGREDIENT':<22} {'QTY':>8}  {'UNIT':<8}  {'BEST STORE':<12}  NOTES")
    print(f"  {'-'*22}  {'-'*8}  {'-'*8}  {'-'*12}  -----")
    for item in shopping:
        qty_str = f"{item['total_quantity']:>8.2f}"
        cost_str = f"${item['estimated_cost']:.2f}" if isinstance(item["estimated_cost"], float) else ""
        notes = item["notes"] + (f" ({cost_str})" if cost_str else "")
        print(f"  {item['ingredient']:<22} {qty_str}  {item['unit']:<8}  {item['best_store']:<12}  {notes}")

    print("═" * 60 + "\n")
    return shopping


# ── Write results back to Google Sheets ──────────────────────────────────────

def write_plan_to_sheets(ss, selected_ids, recipe_names, ing_map, price_map):
    """Write the meal plan and shopping list to their respective tabs."""
    week_start = date.today() - timedelta(days=date.today().weekday())  # Monday

    # ── Meal Plan tab ────────────────────────────────────────────────────────
    plan_ws = ss.worksheet("Meal Plan")
    # Clear old data (keep header row)
    existing = plan_ws.get_all_values()
    if len(existing) > 1:
        plan_ws.delete_rows(2, len(existing))

    rows = []
    for i, rid in enumerate(selected_ids):
        day = DAYS_OF_WEEK[i] if i < 7 else f"Day {i+1}"
        day_date = (week_start + timedelta(days=i)).isoformat()
        rows.append([day_date, day, "Dinner", recipe_names.get(rid, rid), rid, "", ""])

    plan_ws.append_rows(rows)
    print(f"✅ Meal Plan tab updated ({len(rows)} meals)")

    # ── Shopping List tab ────────────────────────────────────────────────────
    shop_ws = ss.worksheet("Shopping List")
    existing = shop_ws.get_all_values()
    if len(existing) > 1:
        shop_ws.delete_rows(2, len(existing))

    shopping = build_shopping_list(selected_ids, ing_map, price_map)
    shop_rows = [
        [
            item["ingredient"],
            item["total_quantity"],
            item["unit"],
            item["best_store"],
            item["unit_price"],
            item["estimated_cost"],
            item["in_pantry"],
            item["notes"],
        ]
        for item in shopping
    ]
    shop_ws.append_rows(shop_rows)
    print(f"✅ Shopping List tab updated ({len(shop_rows)} items)")

    # ── Price Tracker sync ───────────────────────────────────────────────────
    # Add placeholder rows for any shopping-list ingredients that aren't
    # already tracked in the Price Tracker, so users know what to look up.
    try:
        price_ws = ss.worksheet("Price Tracker")
        existing_prices = pd.DataFrame(price_ws.get_all_records())
        already_tracked: set = set()
        if not existing_prices.empty and "ingredient" in existing_prices.columns:
            already_tracked = set(
                existing_prices["ingredient"].astype(str).str.strip().str.lower().dropna()
            )

        new_price_rows = []
        seen_this_batch: set = set()
        for item in shopping:
            ing = item["ingredient"].strip().lower()
            if ing and ing not in already_tracked and ing not in seen_this_batch:
                # Columns: ingredient, store, brand_size, price, qty_amount,
                #          qty_unit, price_per_unit, on_sale, sale_ends, notes
                new_price_rows.append([ing, "", "", "", "", item["unit"], "", "No", "", "Add price"])
                seen_this_batch.add(ing)

        if new_price_rows:
            price_ws.append_rows(new_price_rows)
            print(f"✅ Price Tracker updated ({len(new_price_rows)} new ingredients added)")
    except Exception as e:
        print(f"⚠️  Could not sync Price Tracker: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(spreadsheet_id, num_meals=7, write_back=False, credentials_path="credentials.json"):
    print("🔗 Connecting to Google Sheets...")
    gc = get_client(credentials_path)
    ss = get_spreadsheet(gc, spreadsheet_id)

    print("📋 Loading recipes and prices...")
    recipes_df, ing_df, prices_df = load_data(ss)

    ing_map = build_ingredient_map(ing_df)
    price_map = build_price_map(prices_df)

    recipe_ids = list(recipes_df["recipe_id"].astype(str))
    recipe_names = dict(zip(recipes_df["recipe_id"].astype(str), recipes_df["name"]))

    print(f"   {len(recipe_ids)} recipes loaded, planning {num_meals} meals...")

    # Run the optimizer
    selected_ids = greedy_meal_plan(recipe_ids, ing_map, num_meals)

    # Print to terminal
    shopping = print_plan(selected_ids, recipe_names, ing_map, price_map)

    # Optionally write back to Sheets
    if write_back:
        print("Writing results to Google Sheets...")
        write_plan_to_sheets(ss, selected_ids, recipe_names, ing_map, price_map)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python meal_optimizer.py <spreadsheet_id> [num_meals] [--write]")
        print()
        print("  num_meals  How many dinners to plan (default: 7)")
        print("  --write    Also write the plan back to your Google Sheet")
        sys.exit(1)

    spreadsheet_id = sys.argv[1]
    num_meals = 7
    write_back = "--write" in sys.argv

    for arg in sys.argv[2:]:
        if arg.isdigit():
            num_meals = int(arg)

    run(spreadsheet_id, num_meals=num_meals, write_back=write_back)
