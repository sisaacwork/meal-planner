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


def get_credentials_path():
    return getattr(config, "CREDENTIALS_PATH", "credentials.json") if config else "credentials.json"


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


def get_pantry_set():
    """Return a set of ingredient names currently marked as in-stock in the pantry."""
    df = load_pantry()
    if df.empty or "ingredient" not in df.columns:
        return set()
    in_stock_col = next((c for c in df.columns if "stock" in c.lower()), None)
    if in_stock_col:
        df = df[df[in_stock_col].astype(str).str.lower().isin(["yes", "true", "1", "in stock"])]
    return set(df["ingredient"].astype(str).str.strip().str.lower().dropna())


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

                    if tags:
                        st.markdown(render_tag_badges(tags), unsafe_allow_html=True)

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

                        edited = st.data_editor(
                            edit_df,
                            num_rows="dynamic",
                            use_container_width=True,
                            hide_index=True,
                            key=f"ing_editor_{recipe_id}",
                            column_config={
                                "ingredient": st.column_config.TextColumn(
                                    "Ingredient", help="Edit the ingredient name"
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

        if generate_btn or "meal_plan" in st.session_state:
            if generate_btn:
                with st.spinner("Building your meal plan and saving to Google Sheets…"):
                    try:
                        ratings_map  = load_ratings()
                        pantry_set   = get_pantry_set()
                        ing_map      = build_ingredient_map(ing_df)
                        price_map    = build_price_map(prices_df)
                        recipe_ids   = list(recipes_df["recipe_id"].astype(str))
                        recipe_names = dict(zip(recipes_df["recipe_id"].astype(str), recipes_df["name"]))
                        selected_ids = greedy_meal_plan(recipe_ids, ing_map, num_meals, ratings_map=ratings_map)
                        shopping     = build_shopping_list(selected_ids, ing_map, price_map, pantry_set=pantry_set)

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
        st.markdown(
            "Automatically search **Flipp** (flyer deals) and **Loblaws / No Frills** (regular shelf prices) "
            "for every ingredient on your current shopping list. Results are saved to the **Store Prices** tab "
            "in your Google Sheet. Run this once a week after generating your meal plan."
        )
        st.info("💡 You need to have generated a meal plan first — the scraper reads your Shopping List tab for ingredient names.")

        if st.button("🔄 Fetch prices for all shopping list items", type="primary"):
            from store_scraper import refresh_store_prices
            with st.spinner("Searching Flipp and Loblaws/No Frills — this takes about 30–60 seconds…"):
                try:
                    rows_written, errors = refresh_store_prices(get_ss(), postal_code)
                    if rows_written:
                        st.success(f"✅ Done! Saved **{rows_written}** price entries to the Store Prices tab.")
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
                        options=sorted(store_prices_df["ingredient"].dropna().unique()),
                    )
                    if ing_filter:
                        store_prices_df = store_prices_df[store_prices_df["ingredient"].isin(ing_filter)]
            with sp_col2:
                if "source" in store_prices_df.columns:
                    src_filter = st.multiselect(
                        "Filter by source",
                        options=sorted(store_prices_df["source"].dropna().unique()),
                        default=list(store_prices_df["source"].dropna().unique()),
                    )
                    if src_filter:
                        store_prices_df = store_prices_df[store_prices_df["source"].isin(src_filter)]

            st.dataframe(store_prices_df, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — PANTRY
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🥫  Pantry":
    st.header("🥫 Pantry")
    st.markdown(
        "Keep track of what you already have at home. "
        "Anything marked **In Stock** will be flagged on your shopping list so you know to skip it at the store."
    )

    pantry_df = load_pantry()
    ss = get_ss()

    # ── Ensure Pantry tab exists in Google Sheets ─────────────────────────────
    try:
        pantry_ws = ss.worksheet("Pantry")
    except Exception:
        pantry_ws = ss.add_worksheet(title="Pantry", rows=300, cols=4)
        pantry_ws.append_row(["ingredient", "in_stock", "date_added", "notes"])
        load_pantry.clear()
        pantry_df = pd.DataFrame(columns=["ingredient", "in_stock", "date_added", "notes"])

    # ── Add a new pantry item ─────────────────────────────────────────────────
    with st.expander("➕ Add an item to your pantry", expanded=pantry_df.empty):
        p_col1, p_col2 = st.columns([2, 1])
        with p_col1:
            new_ing   = st.text_input("Ingredient name", placeholder="olive oil, pasta, garlic…", key="pantry_new_ing")
        with p_col2:
            new_notes = st.text_input("Notes (optional)", placeholder="half a bag", key="pantry_new_notes")

        if st.button("Add to Pantry", type="primary"):
            if not new_ing.strip():
                st.warning("Please enter an ingredient name.")
            else:
                try:
                    pantry_ws.append_row([
                        new_ing.strip().lower(),
                        "Yes",
                        date.today().isoformat(),
                        new_notes.strip(),
                    ])
                    load_pantry.clear()
                    st.success(f"✅ **{new_ing.strip()}** added to pantry!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

    st.markdown("---")

    # ── Current pantry items ──────────────────────────────────────────────────
    if pantry_df.empty or "ingredient" not in pantry_df.columns:
        st.info("Your pantry is empty. Add items above and they'll be skipped on your shopping list.")
    else:
        in_stock_count = 0
        if "in_stock" in pantry_df.columns:
            in_stock_count = pantry_df["in_stock"].astype(str).str.lower().isin(["yes", "true", "1"]).sum()
        st.metric("Items in pantry", f"{in_stock_count} in stock / {len(pantry_df)} total")

        # Show pantry as an editable table so users can toggle in_stock and edit notes
        edit_cols = [c for c in ["ingredient", "in_stock", "notes"] if c in pantry_df.columns]
        pantry_edit_df = pantry_df[edit_cols].reset_index(drop=True)

        edited_pantry = st.data_editor(
            pantry_edit_df,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            key="pantry_editor",
            column_config={
                "ingredient": st.column_config.TextColumn("Ingredient"),
                "in_stock":   st.column_config.CheckboxColumn(
                    "In Stock?",
                    help="Tick if you have this at home. Untick when you've used it up.",
                    default=True,
                ),
                "notes":      st.column_config.TextColumn("Notes"),
            },
        )

        if st.button("💾 Save pantry changes"):
            with st.spinner("Saving…"):
                try:
                    # Rewrite the whole sheet to reflect edits
                    all_vals = pantry_ws.get_all_values()
                    header   = all_vals[0] if all_vals else ["ingredient", "in_stock", "date_added", "notes"]

                    # Preserve date_added from the original sheet
                    orig_dates = {}
                    for row in all_vals[1:]:
                        if row:
                            orig_dates[str(row[0]).strip().lower()] = row[2] if len(row) > 2 else ""

                    new_rows = [header]
                    for _, r in edited_pantry.iterrows():
                        ing = str(r.get("ingredient", "")).strip().lower()
                        if not ing:
                            continue
                        in_stock_val = "Yes" if r.get("in_stock") else "No"
                        new_rows.append([
                            ing,
                            in_stock_val,
                            orig_dates.get(ing, date.today().isoformat()),
                            str(r.get("notes", "")).strip(),
                        ])

                    pantry_ws.clear()
                    pantry_ws.update("A1", new_rows)
                    load_pantry.clear()
                    st.success("✅ Pantry updated!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save: {e}")

        st.markdown("---")
        st.caption("To remove an item entirely, clear its ingredient name and save — blank rows are dropped automatically.")
