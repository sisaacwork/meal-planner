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


@st.cache_data(ttl=60, show_spinner="Loading recipes…")
def load_all_data():
    ss = get_sheets_connection()
    if ss is None:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    recipes_df, ing_df, prices_df = load_data(ss)
    return recipes_df, ing_df, prices_df


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🥗 Meal Planner")
    st.markdown("---")
    page = st.radio(
        "Go to",
        ["➕  Add Recipe", "📖  My Recipes", "📅  Generate Meal Plan", "🛒  Shopping List", "💰  Price Tracker"],
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

    if recipes_df.empty:
        st.info("No recipes yet. Head to **Add Recipe** to get started!")
    else:
        st.metric("Recipes saved", len(recipes_df))
        st.markdown("---")

        search = st.text_input("🔍 Search recipes", placeholder="chicken, soup, pasta…")
        if search:
            mask = recipes_df.apply(
                lambda row: search.lower() in str(row.get("name", "")).lower()
                or search.lower() in str(row.get("tags", "")).lower()
                or search.lower() in str(row.get("cuisine", "")).lower(),
                axis=1,
            )
            display_df = recipes_df[mask]
        else:
            display_df = recipes_df

        if display_df.empty:
            st.warning("No recipes match that search.")
        else:
            for _, row in display_df.iterrows():
                recipe_id = str(row.get("recipe_id", ""))
                name      = row.get("name", "Unknown")
                url       = row.get("url", "")
                servings  = row.get("servings", "")
                cuisine   = row.get("cuisine", "")
                tags      = row.get("tags", "")
                added     = row.get("date_added", "")

                with st.expander(f"**{name}**  —  {cuisine}  {('🏷️ ' + tags) if tags else ''}"):
                    c1, c2, c3 = st.columns(3)
                    c1.markdown(f"**Servings:** {servings}")
                    c2.markdown(f"**Added:** {added}")
                    if url:
                        c3.markdown(f"[View original recipe ↗]({url})")

                    recipe_ings = ing_df[ing_df["recipe_id"].astype(str) == recipe_id]
                    if not recipe_ings.empty:
                        st.markdown("**Ingredients:**")
                        show_cols = [c for c in ["ingredient", "quantity", "unit"] if c in recipe_ings.columns]
                        st.dataframe(recipe_ings[show_cols].reset_index(drop=True),
                                     use_container_width=True, hide_index=True)


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
            num_meals   = st.slider("Dinners to plan", 2, min(7, len(recipes_df)), min(7, len(recipes_df)))
            generate_btn = st.button("✨ Generate Plan", type="primary", use_container_width=True)

        if generate_btn or "meal_plan" in st.session_state:
            if generate_btn:
                ing_map      = build_ingredient_map(ing_df)
                price_map    = build_price_map(prices_df)
                recipe_ids   = list(recipes_df["recipe_id"].astype(str))
                recipe_names = dict(zip(recipes_df["recipe_id"].astype(str), recipes_df["name"]))
                selected_ids = greedy_meal_plan(recipe_ids, ing_map, num_meals)
                shopping     = build_shopping_list(selected_ids, ing_map, price_map)

                st.session_state["meal_plan"] = {
                    "selected_ids": selected_ids,
                    "recipe_names": recipe_names,
                    "shopping":     shopping,
                    "ing_map":      ing_map,
                }

            plan         = st.session_state["meal_plan"]
            selected_ids = plan["selected_ids"]
            recipe_names = plan["recipe_names"]
            shopping     = plan["shopping"]
            ing_map      = plan["ing_map"]

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

            m1, m2, m3 = st.columns(3)
            m1.metric("Ingredient overlap",    f"{score:.0%}")
            m2.metric("Shared ingredients",    len(shared))
            m3.metric("Total unique ingredients", len(ingredient_counts))

            if shared:
                with st.expander(f"🔗 {len(shared)} shared ingredients — buy these in bulk"):
                    for ing, recipes in sorted(shared.items()):
                        st.markdown(f"**{ing}** — used in: {', '.join(recipes)}")

            st.markdown("---")
            if st.button("💾 Save this plan to Google Sheets", use_container_width=True):
                with st.spinner("Saving…"):
                    try:
                        ss        = get_ss()
                        price_map = build_price_map(prices_df)
                        from meal_optimizer import write_plan_to_sheets
                        write_plan_to_sheets(ss, selected_ids, recipe_names, ing_map, price_map)
                        load_all_data.clear()
                        st.success("✅ Meal plan and shopping list saved to your Google Sheet!")
                    except Exception as e:
                        st.error(f"Could not save: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 4 — SHOPPING LIST
# ══════════════════════════════════════════════════════════════════════════════
elif page == "🛒  Shopping List":
    st.header("🛒 Shopping List")

    try:
        ss       = get_ss()
        shop_ws  = ss.worksheet("Shopping List")
        shop_df  = pd.DataFrame(shop_ws.get_all_records())
    except Exception:
        shop_df = pd.DataFrame()

    if shop_df.empty:
        st.info("Your shopping list is empty. Generate a meal plan first, then save it.")
    else:
        cost_col = next((c for c in shop_df.columns if "cost" in c.lower()), None)
        if cost_col:
            total = pd.to_numeric(shop_df[cost_col], errors="coerce").sum()
            if total:
                st.metric("Estimated total", f"${total:.2f} CAD")

        st.markdown("---")

        store_col = next((c for c in shop_df.columns if "store" in c.lower()), None)
        if store_col:
            stores       = shop_df[store_col].dropna().unique()
            store_filter = st.multiselect("Filter by store", options=sorted(stores), default=list(stores))
            shop_df      = shop_df[shop_df[store_col].isin(store_filter)]

        st.dataframe(shop_df, use_container_width=True, hide_index=True)

        csv = shop_df.to_csv(index=False)
        st.download_button("⬇️ Download as CSV", data=csv,
                           file_name=f"shopping_list_{date.today()}.csv", mime="text/csv")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — PRICE TRACKER
# ══════════════════════════════════════════════════════════════════════════════
elif page == "💰  Price Tracker":
    st.header("💰 Price Tracker")
    st.markdown("Track prices across your favourite Toronto stores. Update weekly when you check the flyers.")

    postal_code = get_postal_code()

    tab_view, tab_add, tab_flipp = st.tabs(["📋 Current Prices", "➕ Add / Update a Price", "🔍 Search Flipp Deals"])

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

                for deal in deals[:15]:  # Show top 15
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
                                key=f"save_{deal['store']}_{deal['name'][:10]}",
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
