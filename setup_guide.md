# Meal Planner — Setup Guide

This guide walks you through getting everything connected.
It looks like a lot of steps but most of them are one-time setup.

---

## Step 1 — Install Python dependencies

Open your terminal and run:

```bash
pip install -r requirements.txt
```

---

## Step 2 — Create your Google Spreadsheet

1. Go to [Google Sheets](https://sheets.google.com) and create a new blank spreadsheet.
2. Name it something like **Meal Planner**.
3. Copy the spreadsheet's **ID** from the URL bar. It's the long string between `/d/` and `/edit`:

   ```
   https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms/edit
                                          ↑ this whole thing is your Spreadsheet ID
   ```

4. Upload the template: File → Import → Upload `meal_planner_template.xlsx`.
   Choose **Replace spreadsheet** when prompted.
   You should now see 6 tabs: Recipes, Ingredients, Price Tracker, Meal Plan, Shopping List, How To Use.

---

## Step 3 — Set up Google API access (one-time)

This lets the Python scripts read and write your spreadsheet automatically.

### 3a. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Name it `meal-planner` → **Create**

### 3b. Enable the Google Sheets API

1. In the left sidebar: **APIs & Services → Library**
2. Search for **Google Sheets API** → click it → **Enable**
3. Also enable **Google Drive API** the same way (needed for gspread)

### 3c. Create a Service Account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Fill in a name (e.g., `meal-planner-bot`) → **Create and Continue**
4. Skip the optional steps → **Done**

### 3d. Download the credentials file

1. Click on the service account you just created
2. Go to the **Keys** tab → **Add Key → Create new key**
3. Choose **JSON** → **Create**
4. A file downloads automatically — rename it to `credentials.json`
5. Move `credentials.json` into the same folder as these Python scripts

### 3e. Share your spreadsheet with the service account

1. Open `credentials.json` in a text editor and copy the `client_email` value
   (it looks like `meal-planner-bot@your-project.iam.gserviceaccount.com`)
2. In your Google Sheet, click **Share** (top right)
3. Paste the email address and set the role to **Editor** → **Send**

---

## Step 4 — Add your first recipe

```bash
python recipe_ingester.py https://www.allrecipes.com/recipe/12345/chicken-soup YOUR_SPREADSHEET_ID
```

Replace `YOUR_SPREADSHEET_ID` with the ID you copied in Step 2.
The script will print what it found and confirm it was saved.

---

## Step 5 — Add prices (optional but recommended)

Open the **Price Tracker** tab in your spreadsheet.
Add rows for ingredients you commonly buy, with prices from your local stores:
- Loblaws, No Frills, Farm Boy, Longo's — whatever you shop at
- Check the `on_sale` column: set it to `Yes` for current sales
- The `price_per_unit` column calculates automatically (it's a formula)

You can update this weekly when you check the flyers.

---

## Step 6 — Generate a meal plan

Once you have at least 7 recipes saved, run:

```bash
# Print the plan to your terminal
python meal_optimizer.py YOUR_SPREADSHEET_ID

# Print AND write back to your Google Sheet
python meal_optimizer.py YOUR_SPREADSHEET_ID 7 --write
```

The optimizer picks the combination of 7 meals that shares the most ingredients,
then prints a shopping list with the cheapest store for each item.

---

## Troubleshooting

**"Could not scrape that URL"**
→ Not all recipe sites are supported. Try a URL from AllRecipes, Food Network,
  BBC Good Food, NYT Cooking, Serious Eats, or Epicurious.

**"credentials.json not found"**
→ Make sure the file is in the same folder as the Python scripts.

**"No recipes found"**
→ You need to add at least one recipe with `recipe_ingester.py` before optimizing.

**Ingredient names look wrong (e.g., "flour" shows up as "purpose flour")**
→ The ingredient parser is good but not perfect. You can edit the
  `Ingredients` tab in your spreadsheet to fix any names manually.

---

## File overview

| File | What it does |
|------|-------------|
| `sheets_client.py` | Handles the Google Sheets connection (don't edit this) |
| `ingredient_parser.py` | Converts "2 large carrots, diced" into structured data |
| `recipe_ingester.py` | Scrapes a recipe URL and saves it to your sheet |
| `meal_optimizer.py` | Finds the best recipe combination and builds your shopping list |
| `requirements.txt` | Python libraries to install |
| `meal_planner_template.xlsx` | Upload this to Google Sheets to create the right tab structure |
