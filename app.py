"""
app.py — Meal Planner Web App
Run with:  streamlit run app.py
"""

import streamlit as st
import pandas as pd
from collections import defaultdict
from datetime import date

try:
    import config  # exists locally; not present on Streamlit Cloud (uses st.secrets instead)
except ModuleNotFoundError:
    config = None  # type: ignore
from sheets_client import get_client, get_spreadsheet
from recipe_ingester import ingest_recipe, ingest_manual
from meal_optimizer import (
    load_data, build_ingredient_map, build_price_map,
    greedy_meal_plan, build_shopping_list,
)
from flipp_client import TORONTO_STORES, search_flipp, flipp_web_search_url
from unit_converter import convert_to_metric, format_metric

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="🥗 Meal Planner",
    page_icon="🥗",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; }
    .stButton>button { border-radius: 8px; }
    div[data-testid="metric-container"] { background: #f0faf0; border-radius: 10px; padding: 10px; }
</style>
""", unsafe_allow_html=True)


# ── Config helpers (works locally AND on Streamlit Cloud) ─────────────────────
def get_spreadsheet_id():
    if "SPREADSHEET_ID" in st.secrets:
        return st.secrets["SPREADSHEET_ID"]
    return config.SPREADSHEET_ID if config else ""


def get_postal_code():
    if "POSTAL_CODE" in st.secrets:
        return st.secrets["POSTAL_CODE"]
    return getattr(config, "POSTAL_CODE", "M5V3A8") if config else "M5V3A8"


def get_instacart_cookie() -> str:
    if "INSTACART_COOKIE" in st.secrets:
        return st.secrets["INSTACART_COOKIE"]
    return getattr(config, "INSTACART_COOKIE", "") if config else ""


def get_credentials_path():
    return getattr(config, "CREDENTIALS_PATH", "credentials.json") if config else "credentials.json"


def sync_store_prices_to_tracker(ss) -> int:
    """
    After an auto-fetch, copy the best price per (ingredient, store) from the
    'Store Prices' sheet into the 'Price Tracker' sheet so the optimizer and
    the 'Current Prices' tab can use them.

    Logic:
    - Rows in Price Tracker whose notes column contains "auto-fetched" are
      considered managed by this function and will be replaced on each run.
    - Rows entered manually (any other notes value) are never touched.

    Returns the number of rows written.
    """
    try:
        # ── Load Store Prices (the fresh auto-fetch results) ──────────────────
        sp_ws  = ss.worksheet("Store Prices")
        sp_df  = pd.DataFrame(sp_ws.get_all_records())
        if sp_df.empty:
            return 0

        # Coerce price column; drop rows with no price
        sp_df["price"] = pd.to_numeric(sp_df.get("price", pd.Series()), errors="coerce")
        sp_df = sp_df.dropna(subset=["price"])
        if sp_df.empty:
            return 0

        # Keep the cheapest price per (ingredient, store)
        best_df = sp_df.loc[sp_df.groupby(["ingredient", "store"])["price"].idxmin()].copy()

        # ── Load Price Tracker and strip old auto-fetched rows ────────────────
        pt_ws   = ss.worksheet("Price Tracker")
        pt_vals = pt_ws.get_all_values()

        if pt_vals:
            header   = pt_vals[0]
            pt_rows  = pt_vals[1:]
        else:
            header   = ["ingredient", "store", "brand_size", "price",
                        "qty_amount", "qty_unit", "price_per_unit",
                        "on_sale", "sale_ends", "notes"]
            pt_rows  = []

        # Figure out which column is "notes" (last column by default)
        notes_idx = header.index("notes") if "notes" in header else len(header) - 1

        # Keep only manual rows (anything whose notes cell doesn't say auto-fetched)
        manual_rows = [
            r for r in pt_rows
            if "auto-fetched" not in str(r[notes_idx] if notes_idx < len(r) else "").lower()
        ]

        # ── Build the new auto-fetched rows to append ─────────────────────────
        new_rows = []
        for _, row in best_df.iterrows():
            ing    = str(row.get("ingredient", "")).strip().lower()
            store  = str(row.get("store", "")).strip()
            price  = row.get("price", "")
            qty_a  = row.get("qty_amount", 1)
            qty_u  = str(row.get("qty_unit", "whole")).strip()
            ppu    = row.get("price_per_unit", price)
            on_sale = str(row.get("on_sale", "No")).strip()
            sale_ends = str(row.get("sale_ends", "")).strip()
            source    = str(row.get("source", "")).strip()
            prod_name = str(row.get("product_name", "")).strip()

            if not ing or not store:
                continue

            new_rows.append([
                ing,
                store,
                prod_name,          # brand_size
                round(float(price), 2) if price else "",
                qty_a,
                qty_u,
                round(float(ppu), 4) if ppu else "",
                on_sale,
                sale_ends,
                f"auto-fetched ({source})",
            ])

        if not new_rows:
            return 0

        # ── Write: header + manual rows + fresh auto-fetched rows ─────────────
        all_rows = [header] + manual_rows + new_rows
        pt_ws.clear()
        pt_ws.update("A1", all_rows)
        return len(new_rows)

    except Exception as e:
        print(f"[sync_store_prices_to_tracker] {e}")
        return 0


# ── Google Sheets connection ──────────────────────────────────────────────────
@st.cache_resource(show_spinner="Connecting to Google Sheets…")
def get_sheets_connection():
    try:
        gc = get_client(get_credentials_path())
        ss = get_spreadsheet(gc, get_spreadsheet_id())
        return ss
    except Exception:
        return None


def get_ss():
    ss = get_sheets_connection()
    if ss is None:
        st.error(
            "⚠️ Could not connect to Google Sheets. "
            "Check that `credentials.json` exists and `SPREADSHEET_ID` is set in `config.py`."
        )
        st.stop()
    return ss


def save_recipe_tags(ss, recipe_id: str, new_tags: str):
    """Update the tags cell for one recipe in the Recipes sheet."""
    ws         = ss.worksheet("Recipes")
    all_values = ws.get_all_values()
    if not all_values:
        raise ValueError("Recipes sheet is empty.")
    header = [h.lower().strip() for h in all_values[0]]
    try:
        id_col   = header.index("recipe_id") + 1   # gspread is 1-indexed
        tags_col = header.index("tags") + 1
    except ValueError as exc:
        raise ValueError(f"Column not found in Recipes sheet: {exc}") from exc
    for row_num, row in enumerate(all_values[1:], start=2):
        if row and str(row[id_col - 1]).strip() == str(recipe_id).strip():
            ws.update_cell(row_num, tags_col, new_tags.strip())
            return
    raise ValueError(f"Recipe ID '{recipe_id}' not found.")


def delete_recipe(ss, recipe_id: str):
    """
    Permanently delete a recipe and all its associated data from Google Sheets.
    Removes the row from Recipes, all rows from Ingredients, and (if present)
    the row from Ratings.
    """
    # ── Recipes sheet ────────────────────────────────────────────────────────
    rec_ws     = ss.worksheet("Recipes")
    rec_values = rec_ws.get_all_values()
    if rec_values:
        header   = [h.lower().strip() for h in rec_values[0]]
        id_col   = header.index("recipe_id") if "recipe_id" in header else 0
        for row_num, row in enumerate(rec_values[1:], start=2):
            if row and str(row[id_col]).strip() == str(recipe_id).strip():
                rec_ws.delete_rows(row_num)
                break

    # ── Ingredients sheet ────────────────────────────────────────────────────
    ing_ws     = ss.worksheet("Ingredients")
    ing_values = ing_ws.get_all_values()
    rows_to_delete = [
        i + 2
        for i, row in enumerate(ing_values[1:])
        if row and str(row[0]).strip() == str(recipe_id).strip()
    ]
    for row_num in sorted(rows_to_delete, reverse=True):
        ing_ws.delete_rows(row_num)

    # ── Ratings sheet (optional — skip if it doesn't exist) ──────────────────
    try:
        rat_ws     = ss.worksheet("Ratings")
        rat_values = rat_ws.get_all_values()
        if rat_values:
            header   = [h.lower().strip() for h in rat_values[0]]
            id_col   = header.index("recipe_id") if "recipe_id" in header else 0
            for row_num, row in enumerate(rat_values[1:], start=2):
                if row and str(row[id_col]).strip() == str(recipe_id).strip():
                    rat_ws.delete_rows(row_num)
                    break
    except Exception:
        pass  # Ratings sheet may not exist yet — that's fine


def reset_meal_plan(ss):
    """
    Clear the Meal Plan and Shopping List tabs in Google Sheets, and wipe the
    session-state keys that hold the in-memory plan.  Leaves header rows intact.
    """
    for tab_name in ("Meal Plan", "Shopping List"):
        try:
            ws   = ss.worksheet(tab_name)
            rows = ws.get_all_values()
            if len(rows) > 1:
                ws.delete_rows(2, len(rows))
        except Exception:
            pass  # Tab doesn't exist yet — nothing to clear

    # Wipe in-memory state so the Generate page shows the blank form again
    st.session_state.pop("meal_plan", None)
    st.session_state.pop("shopping_checked", None)


def save_ingredient_edits(ss, recipe_id: str, edited_df: pd.DataFrame):
    """
    Replace all ingredient rows for a recipe in Google Sheets with the edited version.
    Strategy: delete existing rows for this recipe (bottom-up), then append new ones.
    """
    ing_ws = ss.worksheet("Ingredients")
    all_values = ing_ws.get_all_values()  # includes header row

    # Find 1-based sheet row numbers that belong to this recipe (skip header at index 0)
    rows_to_delete = [
        i + 2  # +1 for 0-index → 1-index, +1 for header row
        for i, row in enumerate(all_values[1:])
        if row and str(row[0]).strip() == str(recipe_id).strip()
    ]

    # Delete from bottom to top so row numbers don't shift mid-deletion
    for row_num in sorted(rows_to_delete, reverse=True):
        ing_ws.delete_rows(row_num)

    # Append the edited rows (drop any rows the user left completely blank)
    recipe_name = edited_df["recipe_name"].iloc[0] if "recipe_name" in edited_df.columns else ""
    new_rows = []
    for _, r in edited_df.iterrows():
        ingredient = str(r.get("ingredient", "")).strip()
        if not ingredient:
            continue  # skip blank rows
        new_rows.append([
            recipe_id,
            recipe_name,
            ingredient,
            r.get("quantity", 1),
            r.get("unit", "whole"),
            r.get("original", ingredient),  # keep original if present, else use edited name
        ])

    if new_rows:
        ing_ws.append_rows(new_rows)


@st.cache_data(ttl=60, show_spinner="Loading recipes…")
def load_all_data():
    ss = get_sheets_connection()
    if ss is None:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    recipes_df, ing_df, prices_df = load_data(ss)
    return recipes_df, ing_df, prices_df


@st.cache_data(ttl=60)
def load_pantry():
    """Load the Pantry tab. Returns a DataFrame (may be empty)."""
    ss = get_sheets_connection()
    if ss is None:
        return pd.DataFrame()
    try:
        ws = ss.worksheet("Pantry")
        return pd.DataFrame(ws.get_all_records())
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_ratings():
    """Load the Ratings tab. Returns dict of recipe_id → 'like'|'dislike'."""
    ss = get_sheets_connection()
    if ss is None:
        return {}
    try:
        ws = ss.worksheet("Ratings")
        df = pd.DataFrame(ws.get_all_records())
        if df.empty or "recipe_id" not in df.columns or "rating" not in df.columns:
            return {}
        return dict(zip(df["recipe_id"].astype(str), df["rating"].astype(str)))
    except Exception:
        return {}


def save_rating(ss, recipe_id: str, rating: str):
    """Save or overwrite a rating in the Ratings tab."""
    try:
        try:
            ws = ss.worksheet("Ratings")
        except Exception:
            ws = ss.add_worksheet(title="Ratings", rows=300, cols=4)
            ws.append_row(["recipe_id", "rating", "notes", "rated_date"])

        all_vals = ws.get_all_values()
        for i, row in enumerate(all_vals[1:], start=2):
            if row and str(row[0]).strip() == str(recipe_id).strip():
                ws.update_cell(i, 2, rating)
                ws.update_cell(i, 4, date.today().isoformat())
                return
        ws.append_row([recipe_id, rating, "", date.today().isoformat()])
    except Exception as e:
        st.error(f"Could not save rating: {e}")


def get_pantry_map():
    """
    Return a dict of in-stock pantry items:
      { ingredient_name: { "quantity": float|None, "unit": str } }
    Only items where in_stock is truthy are included.
    """
    df = load_pantry()
    if df.empty or "ingredient" not in df.columns:
        return {}
    if "in_stock" in df.columns:
        df = df[df["in_stock"].astype(str).str.lower().isin(["yes", "true", "1"])]
    result = {}
    for _, row in df.iterrows():
        ing = str(row.get("ingredient", "")).strip().lower()
        if not ing:
            continue
        try:
            qty = float(row.get("quantity", "")) if str(row.get("quantity", "")).strip() else None
        except (ValueError, TypeError):
            qty = None
        unit = str(row.get("unit", "")).strip()
        result[ing] = {"quantity": qty, "unit": unit}
    return result


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🥗 Meal Planner")
    st.markdown("---")
    page = st.radio(
        "Go to",
        ["➕  Add Recipe", "📖  My Recipes", "📅  Generate Meal Plan",
         "🛒  Shopping List", "💰  Price Tracker", "🥫  Pantry"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    if st.button("🔄 Refresh data"):
        load_all_data.clear()
        get_sheets_connection.clear()
        st.rerun()
    st.caption("Data auto-refreshes every 60 seconds.")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — ADD RECIPE
# ══════════════════════════════════════════════════════════════════════════════
if page == "➕  Add Recipe":
    st.header("➕ Add a Recipe")
    st.markdown("Paste a link from a supported site, or type in the ingredients yourself.")

    tab_url, tab_manual = st.tabs(["🔗 From a URL", "✏️ Enter Manually"])

    with tab_url:
        st.markdown("Works great with: **AllRecipes, Budget Bytes, BBC Good Food, Food Network, Epicurious, RecipeTin Eats, Simply Recipes**")
        url_input = st.text_input("Recipe URL", placeholder="https://www.allrecipes.com/recipe/...")

        url_col1, url_col2, url_col3 = st.columns(3)
        with url_col1:
            url_tags     = st.text_input("Tags (optional)", placeholder="chicken, weeknight, slow-cooker", key="url_tags")
        with url_col2:
            url_cuisine  = st.text_input("Cuisine (optional)", placeholder="Italian", key="url_cuisine",
                                         help="Leave blank to use the cuisine detected from the page, if any.")
        with url_col3:
            url_servings = st.text_input("Servings (optional)", placeholder="4", key="url_servings",
                                         help="Leave blank to use the yield detected from the page.")

        if st.button("Fetch & Save Recipe", type="primary", key="fetch_btn"):
            if not url_input.strip():
                st.warning("Please paste a recipe URL first.")
            else:
                with st.spinner("Fetching recipe…"):
                    try:
                        recipe, ingredients = ingest_recipe(
                            url_input.strip(),
                            get_spreadsheet_id(),
                            get_credentials_path(),
                            tags=url_tags,
                            cuisine=url_cuisine,
                            servings=url_servings,
                        )
                        load_all_data.clear()
                        st.success(f"✅ **{recipe['name']}** saved! ({len(ingredients)} ingredients)")
                        with st.expander("See parsed ingredients"):
                            ing_df_show = pd.DataFrame(ingredients)[["ingredient", "quantity", "unit", "original"]]
                            st.dataframe(ing_df_show, use_container_width=True, hide_index=True)
                    except SystemExit:
                        st.error(
                            "Couldn't scrape that URL — the site may be blocking automated access. "
                            "Try the **Enter Manually** tab instead."
                        )
                    except Exception as e:
                        st.error(f"Something went wrong: {e}")

    with tab_manual:
        st.markdown("Type or paste the ingredients — one per line, exactly as they appear in the recipe.")

        col1, col2 = st.columns(2)
        with col1:
            recipe_name  = st.text_input("Recipe name *", placeholder="Italian Wedding Soup")
            servings     = st.text_input("Servings", value="4")
        with col2:
            cuisine      = st.text_input("Cuisine (optional)", placeholder="Italian")
            tags         = st.text_input("Tags (optional)", placeholder="soup, chicken, winter")

        recipe_url = st.text_input("Recipe URL (optional — for your reference)", placeholder="https://…")
        ingredients_text = st.text_area(
            "Ingredients (one per line) *",
            height=200,
            placeholder="1 lb ground turkey\n2 large eggs\n1/2 cup breadcrumbs\n4 cups chicken broth\n2 cups baby spinach",
        )

        if st.button("Save Recipe", type="primary", key="manual_btn"):
            if not recipe_name.strip():
                st.warning("Please enter a recipe name.")
            elif not ingredients_text.strip():
                st.warning("Please enter at least one ingredient.")
            else:
                with st.spinner("Saving…"):
                    try:
                        recipe, ingredients = ingest_manual(
                            name=recipe_name,
                            raw_ingredients_text=ingredients_text,
                            spreadsheet_id=get_spreadsheet_id(),
                            url=recipe_url,
                            servings=servings,
                            cuisine=cuisine,
                            tags=tags,
                            credentials_path=get_credentials_path(),
                        )
                        load_all_data.clear()
                        st.success(f"✅ **{recipe['name']}** saved! ({len(ingredients)} ingredients)")
                        with st.expander("See parsed ingredients"):
                            ing_df_show = pd.DataFrame(ingredients)[["ingredient", "quantity", "unit", "original"]]
                            st.dataframe(ing_df_show, use_container_width=True, hide_index=True)
                    except Exception as e:
                        st.error(f"Something went wrong: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — MY RECIPES
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📖  My Recipes":
    st.header("📖 My Recipes")

    recipes_df, ing_df, _ = load_all_data()

    # Filter out blank / accidental test rows (name empty or only symbols)
    if not recipes_df.empty and "name" in recipes_df.columns:
        recipes_df = recipes_df[
            recipes_df["name"].astype(str).str.strip().str.len() >= 2
        ]
        recipes_df = recipes_df[
            ~recipes_df["name"].astype(str).str.strip().str.match(r"^[\*_\-\s]+$")
        ]

    def render_tag_badges(tags_str: str) -> str:
        """Turn a comma-separated tag string into coloured HTML badge chips."""
        if not tags_str or not str(tags_str).strip():
            return ""
        tags = [t.strip() for t in str(tags_str).split(",") if t.strip()]
        badge = (
            '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 10px;'
            'border-radius:12px;font-size:12px;margin-right:4px;'
            'display:inline-block;margin-bottom:4px;">{}</span>'
        )
        return " ".join(badge.format(t) for t in tags)

    if recipes_df.empty:
        st.info("No recipes yet. Head to **Add Recipe** to get started!")
    else:
        ratings_map = load_ratings()
        st.metric("Recipes saved", len(recipes_df))
        st.markdown("---")

        # Collect all unique tags across every recipe for the filter
        all_tags = sorted({
            tag.strip()
            for raw in recipes_df.get("tags", pd.Series(dtype=str)).fillna("")
            for tag in str(raw).split(",")
            if tag.strip()
        })

        col_search, col_tags = st.columns([2, 3])
        with col_search:
            search = st.text_input("🔍 Search by name", placeholder="chicken, soup…")
        with col_tags:
            selected_tags = st.multiselect(
                "🏷️ Filter by tag",
                options=all_tags,
                placeholder="Select tags…",
            )

        display_df = recipes_df.copy()

        if search:
            mask = display_df.apply(
                lambda row: search.lower() in str(row.get("name", "")).lower()
                or search.lower() in str(row.get("cuisine", "")).lower(),
                axis=1,
            )
            display_df = display_df[mask]

        if selected_tags:
            def has_all_tags(row):
                row_tags = [t.strip().lower() for t in str(row.get("tags", "")).split(",")]
                return all(t.lower() in row_tags for t in selected_tags)
            display_df = display_df[display_df.apply(has_all_tags, axis=1)]

        if display_df.empty:
            st.warning("No recipes match those filters.")
        else:
            for _, row in display_df.iterrows():
                recipe_id = str(row.get("recipe_id", ""))
                name      = str(row.get("name", "Unknown")).strip()
                url       = row.get("url", "")
                servings  = row.get("servings", "")
                cuisine   = str(row.get("cuisine", "")).strip()
                tags      = str(row.get("tags", "")).strip()
                added     = row.get("date_added", "")

                # Expander title: name + cuisine only (tags shown as badges inside)
                title = f"**{name}**" + (f"  —  {cuisine}" if cuisine else "")
                with st.expander(title):
                    c1, c2, c3 = st.columns(3)
                    c1.markdown(f"**Servings:** {servings}")
                    c2.markdown(f"**Added:** {added}")
                    if url:
                        c3.markdown(f"[View original recipe ↗]({url})")

                    # ── Tags (display + inline editor) ───────────────────────
                    if tags:
                        st.markdown(render_tag_badges(tags), unsafe_allow_html=True)

                    # Use a toggle instead of a nested expander — nested expanders
                    # break text-cell editing in st.data_editor on the same page.
                    show_tag_editor = st.toggle(
                        "🏷️ Edit tags", key=f"tag_toggle_{recipe_id}", value=False
                    )
                    if show_tag_editor:
                        new_tags_val = st.text_input(
                            "Tags (comma-separated)",
                            value=tags,
                            placeholder="chicken, weeknight, slow-cooker",
                            key=f"tags_input_{recipe_id}",
                            help="Separate tags with commas. They appear as coloured badges and can be used to filter recipes.",
                        )
                        if st.button("💾 Save tags", key=f"save_tags_{recipe_id}"):
                            with st.spinner("Saving…"):
                                try:
                                    save_recipe_tags(get_ss(), recipe_id, new_tags_val)
                                    load_all_data.clear()
                                    st.success("✅ Tags updated!")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Could not save tags: {e}")

                    # ── Rating buttons ────────────────────────────────────────
                    current_rating = ratings_map.get(recipe_id, "none")
                    rating_labels  = {
                        "like":    "👍 Liked",
                        "dislike": "👎 Disliked",
                        "none":    "⭐ Not rated",
                    }
                    st.caption(f"Your rating: **{rating_labels.get(current_rating, '—')}**  ·  Liked recipes are preferred when generating plans; disliked ones are excluded.")
                    r_col1, r_col2, r_col3 = st.columns([1, 1, 1])
                    with r_col1:
                        liked_label = "👍 Liked ✓" if current_rating == "like" else "👍 Like"
                        if st.button(liked_label, key=f"rate_like_{recipe_id}", use_container_width=True):
                            new_rating = "none" if current_rating == "like" else "like"
                            save_rating(get_ss(), recipe_id, new_rating)
                            load_ratings.clear()
                            st.rerun()
                    with r_col2:
                        dislike_label = "👎 Disliked ✓" if current_rating == "dislike" else "👎 Dislike"
                        if st.button(dislike_label, key=f"rate_dislike_{recipe_id}", use_container_width=True):
                            new_rating = "none" if current_rating == "dislike" else "dislike"
                            save_rating(get_ss(), recipe_id, new_rating)
                            load_ratings.clear()
                            st.rerun()
                    with r_col3:
                        if current_rating != "none":
                            if st.button("✕ Clear rating", key=f"rate_clear_{recipe_id}", use_container_width=True):
                                save_rating(get_ss(), recipe_id, "none")
                                load_ratings.clear()
                                st.rerun()

                    st.markdown("---")
                    recipe_ings = ing_df[ing_df["recipe_id"].astype(str) == recipe_id].copy()
                    if not recipe_ings.empty:
                        st.markdown("**Ingredients** — edit names, quantities, or units inline. Use the ＋ row at the bottom to add one, or the trash icon to delete.")

                        UNIT_OPTIONS = [
                            "whole", "g", "kg", "ml", "litre", "cup", "tbsp", "tsp",
                            "lb", "oz", "clove", "can", "pkg", "slice", "bunch",
                            "handful", "pinch", "dash", "sprig",
                        ]

                        edit_cols = [c for c in ["ingredient", "quantity", "unit", "recipe_name"] if c in recipe_ings.columns]
                        edit_df = recipe_ings[edit_cols].reset_index(drop=True)

                        # gspread returns numeric-looking values (e.g. "6") as int,
                        # not str. A mixed-type column confuses data_editor's text
                        # cells — casting to str fixes the "can't type in the cell" bug.
                        edit_df["ingredient"] = edit_df["ingredient"].astype(str)
                        edit_df["unit"]       = edit_df["unit"].astype(str)
                        edit_df["quantity"]   = pd.to_numeric(edit_df["quantity"], errors="coerce").fillna(0.0)

                        edited = st.data_editor(
                            edit_df,
                            num_rows="dynamic",
                            use_container_width=True,
                            hide_index=True,
                            key=f"ing_editor_{recipe_id}",
                            column_config={
                                "ingredient": st.column_config.TextColumn(
                                    "Ingredient",
                                    help="Edit the ingredient name",
                                ),
                                "quantity": st.column_config.NumberColumn(
                                    "Qty", min_value=0, step=0.25, format="%.2f"
                                ),
                                "unit": st.column_config.SelectboxColumn(
                                    "Unit", options=UNIT_OPTIONS
                                ),
                                "recipe_name": None,  # hide this column
                            },
                        )

                        if st.button("💾 Save ingredient changes", key=f"save_ing_{recipe_id}"):
                            with st.spinner("Saving…"):
                                try:
                                    ss = get_ss()
                                    # Carry recipe_name forward for rows added by the user
                                    if "recipe_name" not in edited.columns:
                                        edited["recipe_name"] = name
                                    else:
                                        edited["recipe_name"] = edited["recipe_name"].fillna(name)
                                    save_ingredient_edits(ss, recipe_id, edited)
                                    load_all_data.clear()
                                    st.success("✅ Ingredients updated!")
                                except Exception as e:
                                    st.error(f"Could not save: {e}")

                    # ── Delete recipe ─────────────────────────────────────────
                    st.markdown("---")
                    confirm_key = f"confirm_delete_{recipe_id}"
                    if not st.session_state.get(confirm_key):
                        if st.button(
                            "🗑️ Delete this recipe",
                            key=f"delete_btn_{recipe_id}",
                            help="Permanently removes this recipe and its ingredients from Google Sheets.",
                        ):
                            st.session_state[confirm_key] = True
                            st.rerun()
                    else:
                        st.warning(
                            f"⚠️ Are you sure you want to permanently delete **{name}**? "
                            "This cannot be undone."
                        )
                        d_yes, d_no = st.columns(2)
                        with d_yes:
                            if st.button("Yes, delete it", type="primary", key=f"delete_yes_{recipe_id}"):
                                with st.spinner("Deleting…"):
                                    try:
                                        delete_recipe(get_ss(), recipe_id)
                                        load_all_data.clear()
                                        load_ratings.clear()
                                        st.session_state.pop(confirm_key, None)
                                        st.success(f"✅ **{name}** has been deleted.")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"Could not delete recipe: {e}")
                        with d_no:
                            if st.button("Cancel", key=f"delete_no_{recipe_id}"):
                                st.session_state.pop(confirm_key, None)
                                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — GENERATE MEAL PLAN
# ══════════════════════════════════════════════════════════════════════════════
elif page == "📅  Generate Meal Plan":
    st.header("📅 Generate a Meal Plan")
    st.markdown("Picks the combination of recipes that shares the most ingredients, so you buy less and waste less.")

    recipes_df, ing_df, prices_df = load_all_data()

    if recipes_df.empty:
        st.info("No recipes yet. Head to **Add Recipe** to get started!")
    else:
        col_left, col_right = st.columns([1, 2])

        with col_left:
            st.subheader("Settings")
            max_meals    = max(1, min(7, len(recipes_df)))  # never less than 1
            default_meals = max_meals
            num_meals    = st.slider("Dinners to plan", 1, max_meals, default_meals)
            budget_cap   = st.number_input(
                "Weekly grocery budget (CAD $)",
                min_value=0.0, step=10.0, value=0.0, format="%.2f",
                help="Set to $0 to skip budget checking. The optimizer will warn you if your estimated total exceeds this.",
            )
            generate_btn = st.button("✨ Generate Plan", type="primary", use_container_width=True)
            reset_btn    = st.button("🗑️ Reset week", use_container_width=True,
                                     help="Clears the current meal plan and shopping list from Google Sheets so you can start fresh.")

        if reset_btn:
            with st.spinner("Resetting…"):
                try:
                    reset_meal_plan(get_ss())
                    load_all_data.clear()
                    st.success("✅ Meal plan and shopping list cleared. Ready for a new week!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not reset: {e}")

        if generate_btn or "meal_plan" in st.session_state:
            if generate_btn:
                with st.spinner("Building your meal plan and saving to Google Sheets…"):
                    try:
                        ratings_map  = load_ratings()
                        pantry_map   = get_pantry_map()
                        ing_map      = build_ingredient_map(ing_df)
                        price_map    = build_price_map(prices_df)
                        recipe_ids   = list(recipes_df["recipe_id"].astype(str))
                        recipe_names = dict(zip(recipes_df["recipe_id"].astype(str), recipes_df["name"]))
                        selected_ids = greedy_meal_plan(recipe_ids, ing_map, num_meals, ratings_map=ratings_map)
                        shopping     = build_shopping_list(selected_ids, ing_map, price_map, pantry_map=pantry_map)

                        st.session_state["meal_plan"] = {
                            "selected_ids": selected_ids,
                            "recipe_names": recipe_names,
                            "shopping":     shopping,
                            "ing_map":      ing_map,
                            "budget_cap":   budget_cap,
                        }

                        # Auto-save to Google Sheets so Shopping List tab updates immediately
                        ss = get_ss()
                        from meal_optimizer import write_plan_to_sheets
                        write_plan_to_sheets(ss, selected_ids, recipe_names, ing_map, price_map)
                        # Clear cached data and shopping list checkbox state
                        load_all_data.clear()
                        st.session_state.pop("shopping_checked", None)
                        st.success("✅ Plan saved! Head to **🛒 Shopping List** to see your list.")
                    except Exception as e:
                        st.error(f"Could not generate plan: {e}")
                        st.stop()

            if "meal_plan" not in st.session_state:
                st.stop()

            plan         = st.session_state["meal_plan"]
            selected_ids = plan["selected_ids"]
            recipe_names = plan["recipe_names"]
            shopping     = plan["shopping"]
            ing_map      = plan["ing_map"]
            saved_budget = plan.get("budget_cap", 0.0)

            with col_right:
                st.subheader("Your Week")
                days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
                plan_rows = [{"Day": days[i], "Dinner": recipe_names.get(rid, rid)}
                             for i, rid in enumerate(selected_ids)]
                st.dataframe(pd.DataFrame(plan_rows), use_container_width=True, hide_index=True)

            st.markdown("---")

            ingredient_counts = defaultdict(list)
            for rid in selected_ids:
                for ing in ing_map.get(rid, {}):
                    ingredient_counts[ing].append(recipe_names.get(rid, rid))
            shared = {ing: r for ing, r in ingredient_counts.items() if len(r) > 1}
            score  = len(shared) / len(ingredient_counts) if ingredient_counts else 0

            # ── Budget check ──────────────────────────────────────────────
            est_total = sum(
                item["estimated_cost"] for item in shopping
                if isinstance(item.get("estimated_cost"), (int, float))
            )
            if saved_budget > 0 and est_total > 0:
                over = est_total - saved_budget
                if over > 0:
                    st.error(
                        f"⚠️ Estimated cost **${est_total:.2f}** is **${over:.2f} over** your "
                        f"${saved_budget:.2f} budget. Try reducing the number of dinners or "
                        f"swapping a recipe for one with cheaper ingredients."
                    )
                else:
                    st.success(f"✅ Estimated cost **${est_total:.2f}** — ${abs(over):.2f} under your ${saved_budget:.2f} budget!")

            m1, m2, m3 = st.columns(3)
            m1.metric("Ingredient overlap",      f"{score:.0%}")
            m2.metric("Shared ingredients",      len(shared))
            m3.metric("Total unique ingredients", len(ingredient_counts))

            if shared:
                with st.expander(f"🔗 {len(shared)} shared ingredients — buy these in bulk"):
                    for ing, recipes in sorted(shared.items()):
                        st.markdown(f"**{ing}** — used in: {', '.join(recipes)}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 4 — SHOPPING LIST
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🛒  Shopping List":
    st.header("🛒 Shopping List")
    st.caption("Check items off as you shop — your progress is saved while you have the app open.")

    try:
        ss       = get_ss()
        shop_ws  = ss.worksheet("Shopping List")
        shop_df  = pd.DataFrame(shop_ws.get_all_records())
    except Exception:
        shop_df = pd.DataFrame()

    if shop_df.empty:
        st.info("Your shopping list is empty. Generate a meal plan first, then save it.")
    else:
        # ── Rename columns to clean display headers ───────────────────────────
        col_rename = {
            "ingredient":      "Ingredient",
            "total_quantity":  "Qty",
            "unit":            "Unit",
            "best_store":      "Best Store",
            "unit_price":      "Unit Price",
            "estimated_cost":  "Est. Cost",
            "in_pantry":       "Got it ✓",
            "notes":           "Notes",
        }
        shop_df = shop_df.rename(columns={
            k: v for k, v in col_rename.items() if k in shop_df.columns
        })

        # ── Restore or initialise the "Got it ✓" checkbox state ──────────────
        # We keep it in session_state so ticks survive page interactions
        if "shopping_checked" not in st.session_state:
            # Pre-tick anything already marked in the sheet
            existing = shop_df.get("Got it ✓", pd.Series(["No"] * len(shop_df)))
            st.session_state["shopping_checked"] = (
                existing.astype(str).str.lower().isin(["yes", "true", "1"]).tolist()
            )
        # Pad / trim if list size changed
        n = len(shop_df)
        checked = st.session_state["shopping_checked"]
        if len(checked) != n:
            checked = (checked + [False] * n)[:n]
            st.session_state["shopping_checked"] = checked

        shop_df["Got it ✓"] = checked

        # ── Summary metrics ───────────────────────────────────────────────────
        cost_col = "Est. Cost" if "Est. Cost" in shop_df.columns else None
        if cost_col:
            total     = pd.to_numeric(shop_df[cost_col], errors="coerce").sum()
            remaining = pd.to_numeric(
                shop_df.loc[~shop_df["Got it ✓"], cost_col], errors="coerce"
            ).sum()
            m1, m2, m3 = st.columns(3)
            m1.metric("Total items", n)
            if total:
                m2.metric("Estimated total", f"${total:.2f} CAD")
                m3.metric("Still to buy", f"${remaining:.2f} CAD")

        st.markdown("---")

        # ── Store filter — always show all Toronto stores as options ──────────
        if "Best Store" in shop_df.columns:
            data_stores   = shop_df["Best Store"].dropna().unique().tolist()
            all_store_opts = sorted(set(TORONTO_STORES + data_stores) - {""})
            present_stores = [s for s in all_store_opts if s in data_stores]
            store_filter  = st.multiselect(
                "Filter by store",
                options=all_store_opts,
                default=present_stores,
            )
            filtered_df = shop_df[shop_df["Best Store"].isin(store_filter)]
        else:
            filtered_df = shop_df

        # ── Metric toggle ─────────────────────────────────────────────────────
        use_metric = st.toggle(
            "🔄 Show quantities in metric",
            value=True,
            help="Converts cups/tbsp/tsp/lb/oz → ml/L/g/kg to match Canadian store labels",
        )
        if use_metric and "Qty" in filtered_df.columns and "Unit" in filtered_df.columns:
            def to_metric_row(row):
                try:
                    qty, unit = convert_to_metric(float(row["Qty"]), str(row["Unit"]))
                    row["Qty"]  = qty
                    row["Unit"] = unit
                except Exception:
                    pass
                return row
            filtered_df = filtered_df.apply(to_metric_row, axis=1)

        # ── Editable table with checkboxes ────────────────────────────────────
        col_cfg = {}
        if "Got it ✓" in filtered_df.columns:
            col_cfg["Got it ✓"] = st.column_config.CheckboxColumn(
                "Got it ✓", help="Tick when you've picked this up", default=False
            )
        if "Est. Cost" in filtered_df.columns:
            col_cfg["Est. Cost"] = st.column_config.NumberColumn(
                "Est. Cost", format="$%.2f"
            )
        if "Unit Price" in filtered_df.columns:
            col_cfg["Unit Price"] = st.column_config.NumberColumn(
                "Unit Price", format="$%.4f"
            )

        edited = st.data_editor(
            filtered_df,
            column_config=col_cfg,
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in filtered_df.columns if c != "Got it ✓"],
        )

        # Persist checkbox changes back to session_state
        if "Got it ✓" in edited.columns:
            # Map filtered indices back to full list
            full_checked = list(st.session_state["shopping_checked"])
            for idx, val in zip(filtered_df.index, edited["Got it ✓"].tolist()):
                if idx < len(full_checked):
                    full_checked[idx] = bool(val)
            st.session_state["shopping_checked"] = full_checked

        st.markdown("---")
        col_dl, col_reset, col_clear = st.columns([3, 1, 1])
        with col_dl:
            csv = filtered_df.to_csv(index=False)
            st.download_button(
                "⬇️ Download as CSV",
                data=csv,
                file_name=f"shopping_list_{date.today()}.csv",
                mime="text/csv",
            )
        with col_reset:
            if st.button("↺ Uncheck all"):
                st.session_state["shopping_checked"] = [False] * n
                st.rerun()
        with col_clear:
            if st.button("🗑️ Clear list"):
                st.session_state["confirm_clear_shopping"] = True

        # Two-click confirmation — only appears after pressing "Clear list"
        if st.session_state.get("confirm_clear_shopping"):
            st.warning("⚠️ This will permanently erase your shopping list from Google Sheets. Are you sure?")
            yes_col, no_col = st.columns(2)
            with yes_col:
                if st.button("Yes, clear it", type="primary", key="confirm_clear_yes"):
                    try:
                        clear_ws = get_ss().worksheet("Shopping List")
                        all_rows = clear_ws.get_all_values()
                        if len(all_rows) > 1:
                            clear_ws.delete_rows(2, len(all_rows))
                        st.session_state.pop("shopping_checked", None)
                        st.session_state.pop("confirm_clear_shopping", None)
                        load_all_data.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not clear the list: {e}")
            with no_col:
                if st.button("Cancel", key="confirm_clear_no"):
                    st.session_state.pop("confirm_clear_shopping", None)
                    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — PRICE TRACKER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "💰  Price Tracker":
    st.header("💰 Price Tracker")
    st.markdown("Track prices across your favourite Toronto stores. Update weekly when you check the flyers.")

    postal_code = get_postal_code()

    tab_view, tab_add, tab_flipp, tab_auto = st.tabs([
        "📋 Current Prices", "➕ Add / Update a Price",
        "🔍 Search Flipp Deals", "🔄 Auto-fetch Store Prices",
    ])

    # ── Tab 1: View current prices ────────────────────────────────────────────
    with tab_view:
        try:
            ss         = get_ss()
            price_ws   = ss.worksheet("Price Tracker")
            prices_df  = pd.DataFrame(price_ws.get_all_records())
        except Exception:
            prices_df = pd.DataFrame()

        if prices_df.empty:
            st.info("No prices saved yet. Use the **Add / Update a Price** tab to get started.")
        else:
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                on_sale_col  = next((c for c in prices_df.columns if "sale" in c.lower() and "end" not in c.lower()), None)
                show_sales   = st.checkbox("Show only items on sale")
                if show_sales and on_sale_col:
                    prices_df = prices_df[prices_df[on_sale_col].astype(str).str.lower().isin(["yes","true","1"])]
            with col_f2:
                store_col = next((c for c in prices_df.columns if "store" in c.lower()), None)
                if store_col:
                    stores       = sorted(prices_df[store_col].dropna().unique())
                    store_filter = st.multiselect("Filter by store", options=stores, default=stores)
                    prices_df    = prices_df[prices_df[store_col].isin(store_filter)]

            st.dataframe(prices_df, use_container_width=True, hide_index=True)

            st.markdown("---")
            if not st.session_state.get("confirm_clear_prices"):
                if st.button("🗑️ Clear all saved prices", help="Removes every row from the Price Tracker sheet so you can start fresh."):
                    st.session_state["confirm_clear_prices"] = True
                    st.rerun()
            else:
                st.warning("⚠️ This will permanently delete **all** saved prices from Google Sheets. Are you sure?")
                cp_yes, cp_no = st.columns(2)
                with cp_yes:
                    if st.button("Yes, clear prices", type="primary", key="confirm_clear_prices_yes"):
                        try:
                            pw = get_ss().worksheet("Price Tracker")
                            all_vals = pw.get_all_values()
                            if len(all_vals) > 1:
                                pw.delete_rows(2, len(all_vals))
                            load_all_data.clear()
                            st.session_state.pop("confirm_clear_prices", None)
                            st.success("✅ Price Tracker cleared.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not clear prices: {e}")
                with cp_no:
                    if st.button("Cancel", key="confirm_clear_prices_no"):
                        st.session_state.pop("confirm_clear_prices", None)
                        st.rerun()

    # ── Tab 2: Add / update a price ───────────────────────────────────────────
    with tab_add:
        st.markdown("Fill in the details below to add or update a price in your tracker.")

        col1, col2 = st.columns(2)
        with col1:
            ingredient_name = st.text_input("Ingredient *", placeholder="carrots")
            store_name      = st.selectbox(
                "Store *",
                options=TORONTO_STORES + ["Other"],
            )
            if store_name == "Other":
                store_name = st.text_input("Enter store name", placeholder="My Local Grocery")

        with col2:
            brand_size  = st.text_input("Brand / size", placeholder="1 kg bag")
            price_cad   = st.number_input("Price (CAD $)", min_value=0.0, step=0.01, format="%.2f")
            qty_amount  = st.number_input("Quantity (number)", min_value=0.0, step=0.1, value=1.0)
            qty_unit    = st.selectbox("Unit", ["g", "kg", "ml", "L", "oz", "lb", "whole", "bunch", "pkg"])

        col3, col4 = st.columns(2)
        with col3:
            on_sale     = st.checkbox("Currently on sale?")
        with col4:
            sale_ends   = st.date_input("Sale ends", value=None) if on_sale else None

        notes = st.text_input("Notes (optional)", placeholder="e.g. Organic, Buy 2 get 10% off")

        if st.button("💾 Save Price", type="primary"):
            if not ingredient_name.strip():
                st.warning("Please enter an ingredient name.")
            elif price_cad <= 0:
                st.warning("Please enter a price greater than $0.")
            else:
                with st.spinner("Saving…"):
                    try:
                        ss        = get_ss()
                        price_ws  = ss.worksheet("Price Tracker")
                        ppu       = round(price_cad / qty_amount, 4) if qty_amount > 0 else ""
                        new_row   = [
                            ingredient_name.strip().lower(),
                            store_name,
                            brand_size,
                            price_cad,
                            qty_amount,
                            qty_unit,
                            ppu,
                            "Yes" if on_sale else "No",
                            str(sale_ends) if sale_ends else "",
                            notes,
                        ]
                        price_ws.append_row(new_row)
                        load_all_data.clear()
                        st.success(f"✅ Price saved: **{ingredient_name}** at **{store_name}** for **${price_cad:.2f}**")
                    except Exception as e:
                        st.error(f"Could not save: {e}")

    # ── Tab 3: Search Flipp ───────────────────────────────────────────────────
    with tab_flipp:
        st.markdown(f"Search current Canadian flyer deals via **Flipp**. Using postal code **{postal_code}** — update this in `config.py` if needed.")

        search_query = st.text_input("Search for an ingredient", placeholder="chicken breast, carrots, pasta…")

        col_search, col_link = st.columns([1, 1])
        with col_search:
            search_btn = st.button("🔍 Search Flipp Deals", type="primary")
        with col_link:
            if search_query:
                st.markdown(f"[Open Flipp.com search ↗]({flipp_web_search_url(search_query, postal_code)})")

        if search_btn and search_query:
            with st.spinner(f"Searching Flipp for '{search_query}'…"):
                deals = search_flipp(search_query, postal_code)

            if not deals:
                st.info(
                    f"No flyer deals found for **{search_query}** right now, or Flipp may be temporarily unavailable. "
                    f"Try [searching directly on Flipp.com]({flipp_web_search_url(search_query, postal_code)})."
                )
            else:
                st.success(f"Found **{len(deals)}** deals for **{search_query}**")
                st.markdown("---")

                for i, deal in enumerate(deals[:15]):  # Show top 15
                    with st.container():
                        d_col1, d_col2, d_col3 = st.columns([3, 2, 2])

                        with d_col1:
                            st.markdown(f"**{deal['name']}**")
                            if deal["unit"]:
                                st.caption(deal["unit"])

                        with d_col2:
                            st.markdown(f"🏪 {deal['store']}")
                            if deal["valid_until"]:
                                st.caption(f"Until: {deal['valid_until']}")

                        with d_col3:
                            st.markdown(f"### {deal['price_text']}")
                            if deal.get("price") and st.button(
                                "➕ Save this price",
                                key=f"save_flipp_{i}",
                            ):
                                try:
                                    ss       = get_ss()
                                    price_ws = ss.worksheet("Price Tracker")
                                    ppu      = round(deal["price"] / 1, 4)  # qty=1, user can refine
                                    new_row  = [
                                        search_query.strip().lower(),
                                        deal["store"],
                                        deal["name"],
                                        deal["price"],
                                        1,
                                        "whole",
                                        ppu,
                                        "Yes",
                                        deal.get("valid_until", ""),
                                        "From Flipp",
                                    ]
                                    price_ws.append_row(new_row)
                                    load_all_data.clear()
                                    st.success("Saved!")
                                except Exception as e:
                                    st.error(f"Could not save: {e}")

                        st.divider()

    # ── Tab 4: Auto-fetch store prices ────────────────────────────────────────
    with tab_auto:
        from instacart_client import cookie_looks_configured, INSTACART_RETAILERS

        instacart_cookie  = get_instacart_cookie()
        instacart_enabled = cookie_looks_configured(instacart_cookie)

        st.markdown(
            "Automatically search **Instacart** (Farm Boy, Longo's, Metro, Sobeys & more), "
            "**Flipp** (flyer deals), and **Loblaws / No Frills** (PC Express) "
            "for every ingredient on your shopping list. "
            "Results are saved to the **Store Prices** tab. Run once a week after generating your meal plan."
        )
        st.info("💡 Generate a meal plan first — the scraper reads your Shopping List tab for ingredient names.")

        # ── Instacart setup instructions (shown when cookie not yet configured) ──
        if not instacart_enabled:
            with st.expander("🔑 Set up Instacart (recommended — covers Farm Boy, Longo's, Metro, Sobeys & more)"):
                st.markdown("""
Instacart has real-time shelf prices for stores that don't publish their own API.
To enable it, you need to copy a session cookie from your browser once.
The cookie stays valid for **2–4 weeks**, then you repeat these steps.

**Steps:**
1. Log in to [instacart.ca](https://www.instacart.ca) in Chrome or Firefox.
2. Open **DevTools** (press `F12` or right-click → Inspect).
3. Go to the **Network** tab, then search for any grocery item on Instacart.
4. In the Network tab, click on a request to `instacart.ca` (look for one named **product_search**).
5. Under **Request Headers**, find the line that starts with **`Cookie:`** — copy the full value (it will be very long).
6. Add it to your `config.py`:
```python
INSTACART_COOKIE = "paste_the_long_cookie_string_here"
```
Or add it to your Streamlit secrets file:
```toml
INSTACART_COOKIE = "paste_the_long_cookie_string_here"
```
7. Restart the app and this notice will disappear.

**Stores covered:** """ + ", ".join(INSTACART_RETAILERS.keys()))
        else:
            st.success(f"✅ Instacart connected — covering {len(INSTACART_RETAILERS)} stores: {', '.join(INSTACART_RETAILERS.keys())}")

        st.markdown("---")

        if st.button("🔄 Fetch prices for all shopping list items", type="primary"):
            from store_scraper import refresh_store_prices
            spinner_msg = (
                "Searching Instacart, Flipp, and Loblaws/No Frills — this takes about 60–90 seconds…"
                if instacart_enabled
                else "Searching Flipp and Loblaws/No Frills — this takes about 30–60 seconds…"
            )
            with st.spinner(spinner_msg):
                try:
                    rows_written, errors, source_counts = refresh_store_prices(
                        get_ss(), postal_code, instacart_cookie
                    )
                    if rows_written:
                        parts = [f"**{v}** from {k}" for k, v in source_counts.items() if v > 0]
                        st.success(f"✅ Saved **{rows_written}** price entries — {', '.join(parts)}.")

                        # Warn specifically if Instacart was enabled but returned nothing
                        if instacart_enabled and source_counts.get("Instacart", 0) == 0:
                            st.warning(
                                "⚠️ Instacart returned 0 results even though a cookie is configured. "
                                "The cookie may have expired — log out of instacart.ca, log back in, "
                                "and copy a fresh cookie from DevTools."
                            )

                        # Sync fetched prices into Price Tracker so they show up
                        # in 'Current Prices' and can be used by the meal optimizer
                        synced = sync_store_prices_to_tracker(get_ss())
                        if synced:
                            st.info(f"🔄 {synced} prices synced to your Price Tracker.")
                        load_all_data.clear()

                    if errors:
                        with st.expander(f"⚠️ {len(errors)} warning(s)"):
                            for err in errors:
                                st.caption(err)
                    if not rows_written and not errors:
                        st.warning("Nothing was saved. Is your shopping list empty? Generate a meal plan first.")
                except Exception as e:
                    st.error(f"Could not fetch prices: {e}")

        st.markdown("---")
        st.subheader("Store Prices (last fetch)")

        try:
            store_prices_ws = get_ss().worksheet("Store Prices")
            store_prices_df = pd.DataFrame(store_prices_ws.get_all_records())
            # gspread can return numeric-looking cells as int — cast text columns to str
            for _col in ("ingredient", "store", "product_name", "qty_unit",
                         "on_sale", "sale_ends", "scraped_date", "source"):
                if _col in store_prices_df.columns:
                    store_prices_df[_col] = store_prices_df[_col].astype(str)
            # Numeric columns may contain empty strings (e.g. Flipp rows with no
            # price) — pd.to_numeric with errors='coerce' turns them into NaN
            # instead of raising ArrowInvalid when Streamlit serialises the df.
            for _col in ("price", "qty_amount", "price_per_unit"):
                if _col in store_prices_df.columns:
                    store_prices_df[_col] = pd.to_numeric(
                        store_prices_df[_col], errors="coerce"
                    )
        except Exception:
            store_prices_df = pd.DataFrame()

        if store_prices_df.empty:
            st.caption("No data yet — hit the button above to fetch prices.")
        else:
            # Filter controls
            sp_col1, sp_col2 = st.columns(2)
            with sp_col1:
                if "ingredient" in store_prices_df.columns:
                    ing_filter = st.multiselect(
                        "Filter by ingredient",
                        options=sorted(store_prices_df["ingredient"].dropna().astype(str).unique()),
                    )
                    if ing_filter:
                        store_prices_df = store_prices_df[store_prices_df["ingredient"].isin(ing_filter)]
            with sp_col2:
                if "source" in store_prices_df.columns:
                    src_filter = st.multiselect(
                        "Filter by source",
                        options=sorted(store_prices_df["source"].dropna().astype(str).unique()),
                        default=list(store_prices_df["source"].dropna().astype(str).unique()),
                    )
                    if src_filter:
                        store_prices_df = store_prices_df[store_prices_df["source"].isin(src_filter)]

            st.dataframe(store_prices_df, use_container_width=True, hide_index=True)

            st.markdown("---")
            if not st.session_state.get("confirm_clear_store_prices"):
                if st.button("🗑️ Clear store prices", help="Removes all rows from the Store Prices sheet. Useful before a fresh weekly fetch."):
                    st.session_state["confirm_clear_store_prices"] = True
                    st.rerun()
            else:
                st.warning("⚠️ This will permanently delete all auto-fetched store prices. Are you sure?")
                sp_yes, sp_no = st.columns(2)
                with sp_yes:
                    if st.button("Yes, clear store prices", type="primary", key="confirm_clear_store_prices_yes"):
                        try:
                            spw = get_ss().worksheet("Store Prices")
                            all_vals = spw.get_all_values()
                            if len(all_vals) > 1:
                                spw.delete_rows(2, len(all_vals))
                            st.session_state.pop("confirm_clear_store_prices", None)
                            st.success("✅ Store Prices cleared. Hit the fetch button above to pull fresh data.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not clear store prices: {e}")
                with sp_no:
                    if st.button("Cancel", key="confirm_clear_store_prices_no"):
                        st.session_state.pop("confirm_clear_store_prices", None)
                        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — PANTRY
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🥫  Pantry":
    st.header("🥫 Pantry")
    st.markdown(
        "Track what you already have at home — and how much. "
        "When you generate a meal plan, the shopping list automatically subtracts what's in your pantry. "
        "If you have **enough** of something, it's pre-ticked as 'Got it'. "
        "If you only have **some** of it, it shows the reduced quantity you still need to buy."
    )

    pantry_df = load_pantry()
    ss        = get_ss()

    PANTRY_UNIT_OPTIONS = [
        "ml", "L", "g", "kg", "oz", "lb",
        "cup", "tbsp", "tsp",
        "whole", "clove", "can", "pkg", "slice", "bunch",
    ]
    PANTRY_HEADER = ["ingredient", "in_stock", "quantity", "unit", "date_added", "notes"]

    # ── Ensure Pantry tab exists and has the right columns ────────────────────
    try:
        pantry_ws = ss.worksheet("Pantry")
        # Migrate old sheets that don't yet have quantity/unit columns
        existing_header = pantry_ws.row_values(1)
        if "quantity" not in existing_header:
            # Rebuild header without touching data rows
            pantry_ws.clear()
            all_old = pantry_df.copy()
            pantry_ws.append_row(PANTRY_HEADER)
            for _, old_row in all_old.iterrows():
                pantry_ws.append_row([
                    str(old_row.get("ingredient", "")).strip().lower(),
                    "Yes" if str(old_row.get("in_stock", "Yes")).lower() in ["yes","true","1"] else "No",
                    "",   # quantity — blank for migrated rows
                    "",   # unit — blank for migrated rows
                    str(old_row.get("date_added", date.today().isoformat())),
                    str(old_row.get("notes", "")),
                ])
            load_pantry.clear()
            pantry_df = load_pantry()
    except Exception:
        pantry_ws = ss.add_worksheet(title="Pantry", rows=300, cols=6)
        pantry_ws.append_row(PANTRY_HEADER)
        load_pantry.clear()
        pantry_df = pd.DataFrame(columns=PANTRY_HEADER)

    # ── Add a new pantry item ─────────────────────────────────────────────────
    with st.expander("➕ Add an item to your pantry", expanded=pantry_df.empty):
        p_c1, p_c2, p_c3, p_c4 = st.columns([3, 1, 1, 2])
        with p_c1:
            new_ing  = st.text_input("Ingredient", placeholder="olive oil, pasta, garlic…", key="pantry_new_ing")
        with p_c2:
            new_qty  = st.number_input("Qty on hand", min_value=0.0, step=0.5, value=0.0, key="pantry_new_qty",
                                       help="Leave at 0 to mean 'I have some, unsure of amount'")
        with p_c3:
            new_unit = st.selectbox("Unit", PANTRY_UNIT_OPTIONS, key="pantry_new_unit")
        with p_c4:
            new_notes = st.text_input("Notes (optional)", placeholder="back of cupboard", key="pantry_new_notes")

        if st.button("Add to Pantry", type="primary"):
            if not new_ing.strip():
                st.warning("Please enter an ingredient name.")
            else:
                try:
                    pantry_ws.append_row([
                        new_ing.strip().lower(),
                        "Yes",
                        new_qty if new_qty > 0 else "",
                        new_unit if new_qty > 0 else "",
                        date.today().isoformat(),
                        new_notes.strip(),
                    ])
                    load_pantry.clear()
                    qty_str = f"{new_qty} {new_unit}" if new_qty > 0 else "some (quantity unspecified)"
                    st.success(f"✅ **{new_ing.strip()}** added — {qty_str}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

    st.markdown("---")

    # ── Current pantry items ──────────────────────────────────────────────────
    if pantry_df.empty or "ingredient" not in pantry_df.columns:
        st.info("Your pantry is empty. Add items above and they'll be used when generating your shopping list.")
    else:
        in_stock_count = 0
        if "in_stock" in pantry_df.columns:
            in_stock_count = int(pantry_df["in_stock"].astype(str).str.lower().isin(["yes", "true", "1"]).sum())
        st.metric("Items tracked", f"{in_stock_count} in stock / {len(pantry_df)} total")

        # Build the display DataFrame — ensure quantity and unit columns exist
        for col in ["quantity", "unit"]:
            if col not in pantry_df.columns:
                pantry_df[col] = ""

        edit_cols      = [c for c in ["ingredient", "in_stock", "quantity", "unit", "notes"] if c in pantry_df.columns]
        pantry_edit_df = pantry_df[edit_cols].reset_index(drop=True)

        # Coerce quantity to numeric so the NumberColumn renders properly
        pantry_edit_df["quantity"] = pd.to_numeric(pantry_edit_df["quantity"], errors="coerce")

        st.caption("✏️ Edit any cell directly. Use the **trash icon** on the left to delete a row. Hit **Save** when done.")

        edited_pantry = st.data_editor(
            pantry_edit_df,
            use_container_width=True,
            hide_index=False,       # show row index so trash icon appears
            num_rows="dynamic",     # enables the trash / add-row controls
            key="pantry_editor",
            column_config={
                "ingredient": st.column_config.TextColumn(
                    "Ingredient",
                    help="Ingredient name (lowercase matches best)",
                ),
                "in_stock": st.column_config.CheckboxColumn(
                    "In Stock?",
                    help="Un-tick when you've used it up — it'll be treated as not in the pantry.",
                    default=True,
                ),
                "quantity": st.column_config.NumberColumn(
                    "Qty on hand",
                    help="How much you have right now. Leave blank if you don't know the exact amount.",
                    min_value=0.0,
                    step=0.5,
                    format="%.2f",
                ),
                "unit": st.column_config.SelectboxColumn(
                    "Unit",
                    options=PANTRY_UNIT_OPTIONS,
                    help="Unit for the quantity above.",
                ),
                "notes": st.column_config.TextColumn("Notes"),
            },
        )

        if st.button("💾 Save pantry changes", type="primary"):
            with st.spinner("Saving…"):
                try:
                    # Preserve date_added for existing rows
                    all_vals   = pantry_ws.get_all_values()
                    orig_dates = {}
                    for row in all_vals[1:]:
                        if row:
                            orig_dates[str(row[0]).strip().lower()] = row[4] if len(row) > 4 else ""

                    new_rows = [PANTRY_HEADER]
                    for _, r in edited_pantry.iterrows():
                        ing = str(r.get("ingredient", "")).strip().lower()
                        if not ing:
                            continue   # skip blank / deleted rows
                        qty_val      = r.get("quantity", "")
                        qty_str      = str(qty_val) if pd.notna(qty_val) and qty_val != "" else ""
                        in_stock_val = "Yes" if r.get("in_stock") else "No"
                        new_rows.append([
                            ing,
                            in_stock_val,
                            qty_str,
                            str(r.get("unit", "")).strip(),
                            orig_dates.get(ing, date.today().isoformat()),
                            str(r.get("notes", "")).strip(),
                        ])

                    pantry_ws.clear()
                    pantry_ws.update("A1", new_rows)
                    load_pantry.clear()
                    st.success(f"✅ Pantry saved — {len(new_rows) - 1} items.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

        st.markdown("---")
        st.info(
            "💡 **How quantities work on your shopping list:**\n\n"
            "- If you have **more than enough** (e.g. 500 ml olive oil, recipe needs 200 ml) → item is pre-ticked 'Got it'.\n"
            "- If you have **some but not enough** (e.g. 100 ml, recipe needs 300 ml) → shopping list shows the remaining 200 ml needed.\n"
            "- If **no quantity is entered** → item is treated as fully covered and pre-ticked."
        )
