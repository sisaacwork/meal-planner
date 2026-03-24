"""
ingredient_parser.py
--------------------
Converts a raw ingredient string like "2 large carrots, peeled and diced"
into structured data: { quantity, unit, ingredient, original }.

This is the trickiest part of the app because recipe sites write ingredients
in wildly inconsistent ways. This parser handles the most common formats.
"""

import re


# Map of every common way to write a unit → its canonical (standard) form
UNIT_ALIASES = {
    # Volume
    "cup": "cup", "cups": "cup", "c": "cup",
    "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "tbs": "tbsp", "tb": "tbsp",
    "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp", "ts": "tsp",
    "litre": "litre", "litres": "litre", "liter": "litre", "liters": "litre", "l": "litre",
    "millilitre": "ml", "millilitres": "ml", "milliliter": "ml", "milliliters": "ml", "ml": "ml",
    "fluid ounce": "fl oz", "fluid ounces": "fl oz", "fl oz": "fl oz",
    # Weight
    "gram": "g", "grams": "g", "g": "g",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg",
    "pound": "lb", "pounds": "lb", "lb": "lb", "lbs": "lb",
    "ounce": "oz", "ounces": "oz", "oz": "oz",
    # Count / size descriptors that aren't real units
    "clove": "clove", "cloves": "clove",
    "can": "can", "cans": "can",
    "package": "pkg", "packages": "pkg", "pkg": "pkg",
    "slice": "slice", "slices": "slice",
    "sprig": "sprig", "sprigs": "sprig",
    "bunch": "bunch", "bunches": "bunch",
    "handful": "handful", "handfuls": "handful",
    "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash",
}

# Words that describe HOW to prep the ingredient — not part of the ingredient name
PREP_WORDS = {
    "diced", "chopped", "minced", "sliced", "roughly", "finely", "thinly", "coarsely",
    "grated", "peeled", "trimmed", "halved", "quartered", "crushed", "ground", "torn",
    "shredded", "julienned", "cubed", "mashed", "crumbled", "beaten", "whisked",
    "fresh", "frozen", "dried", "canned", "cooked", "raw", "uncooked", "thawed",
    "boneless", "skinless", "skin-on", "bone-in",
    "large", "small", "medium", "extra", "jumbo",
    "packed", "heaped", "leveled", "level", "rounded",
    "about", "approximately", "roughly", "optional",
    "ripe", "overripe", "firm",
    "thick", "thin",
    "baby", "young",
    "hot", "cold", "room", "temperature",
}

# Trailing phrases that add context but aren't part of the ingredient name.
# These are stripped from the end of the ingredient before further parsing.
TRAILING_PHRASES = [
    "or to taste",
    "to taste",
    "as needed",
    "as required",
    "for serving",
    "for garnish",
    "to serve",
    "to season",
    "if desired",
    "if needed",
    "for topping",
    "for decoration",
    "plus more for serving",
    "plus more to taste",
    "plus more as needed",
    "plus more",
]

# Unicode fractions and their decimal equivalents
UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3,
    "¼": 0.25, "¾": 0.75,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}


def _strip_trailing_phrases(text: str) -> str:
    """
    Remove flavour-only suffixes like 'to taste', 'for garnish', 'as needed'
    from the end of an ingredient string.
    """
    t = text.strip()
    t_lower = t.lower()
    for phrase in TRAILING_PHRASES:
        if t_lower.endswith(phrase):
            t = t[: -len(phrase)].strip().rstrip(",").rstrip(";").strip()
            t_lower = t.lower()
            break  # only one trailing phrase expected
    return t


def _parse_quantity(text: str) -> float:
    """
    Turn a quantity string into a float.
    Handles: "2", "1/2", "1 1/2", "2.5", "½", "1½"
    """
    text = text.strip()

    # Replace unicode fractions with their decimal string
    for char, val in UNICODE_FRACTIONS.items():
        text = text.replace(char, f" {val} ")

    text = text.strip()

    # Mixed number: "1 1/2"
    mixed = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", text)
    if mixed:
        whole = int(mixed.group(1))
        num = int(mixed.group(2))
        den = int(mixed.group(3))
        return whole + num / den

    # Simple fraction: "1/2"
    fraction = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
    if fraction:
        return int(fraction.group(1)) / int(fraction.group(2))

    # Range: "2-3" → take the lower bound
    rng = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", text)
    if rng:
        return float(rng.group(1))

    try:
        return float(text)
    except ValueError:
        return 1.0


def _normalize_name(text: str) -> str:
    """
    Strip prep words, trailing phrases, punctuation, and parenthetical notes
    from an ingredient name.
    e.g. "large carrots, peeled (about 200g) to taste" → "carrots"
    """
    # Strip trailing phrases first (before comma logic removes useful context)
    text = _strip_trailing_phrases(text)

    # Remove parenthetical notes
    text = re.sub(r"\(.*?\)", "", text)

    # Remove everything after a comma
    text = text.split(",")[0]

    # Split into words, remove prep words and linking words, rejoin
    SKIP = PREP_WORDS | {"of", "for", "to", "and", "or", "with", "the", "a", "an"}
    words = text.lower().split()
    cleaned = [w.strip(".,;:") for w in words if w.strip(".,;:") not in SKIP]

    result = " ".join(cleaned).strip()

    # If every remaining token is purely numeric / fractional, this is not a real name
    if result and re.fullmatch(r"[\d.\s/½⅓⅔¼¾⅛⅜⅝⅞–-]+", result):
        return ""

    return result


def parse_ingredient(raw: str) -> dict:
    """
    Parse a raw ingredient string into structured components.

    Returns a dict with:
      - original  : the unchanged input string
      - quantity  : float (e.g. 2.0, 0.5)
      - unit      : canonical unit string, or "whole" if no unit found
      - ingredient: cleaned ingredient name (empty string if parsing fails)
    """
    original = raw.strip()
    text = original

    # Strip trailing phrases from the full line before any other processing
    text = _strip_trailing_phrases(text)

    # ── Step 1: Extract quantity from the start ──────────────────────────────
    quantity = 1.0

    # Handle "a" or "an" at the start (e.g. "a pinch of salt")
    a_match = re.match(r"^an?\s+", text, re.IGNORECASE)
    if a_match:
        quantity = 1.0
        text = text[a_match.end():]

    # Handle "1kg", "500g" — number glued to unit with no space
    glued_match = re.match(r"^(\d+(?:\.\d+)?)\s*(g|kg|ml|l)\b\s*", text, re.IGNORECASE)
    if glued_match and not a_match:
        quantity = float(glued_match.group(1))
        unit_str = glued_match.group(2).lower()
        unit = UNIT_ALIASES.get(unit_str, "whole")
        text = text[glued_match.end():].strip()
        ingredient = _normalize_name(text)
        if not ingredient:
            ingredient = _normalize_name(original)
        return {"original": original, "quantity": round(quantity, 4), "unit": unit, "ingredient": ingredient}

    # Handle ranges like "6-8" at the start — keep lower bound
    range_match = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s+", text)
    if range_match and not a_match:
        quantity = float(range_match.group(1))
        text = text[range_match.end():].strip()
    else:
        # Standard quantity: "2", "1/2", "1 1/2", "2.5", "½", "2½"
        qty_pattern = (
            r"^"
            r"([\d]+(?:\.\d+)?(?:\s*/\s*[\d]+)?"  # integer, decimal, or fraction
            r"(?:\s+[\d]+\s*/\s*[\d]+)?"           # optional mixed number part
            r"(?:\s*[½⅓⅔¼¾⅛⅜⅝⅞]+)?)"             # optional trailing unicode fraction
            r"\s*"
        )
        qty_match = re.match(qty_pattern, text)
        if qty_match and qty_match.group(1).strip() and not a_match:
            quantity = _parse_quantity(qty_match.group(1))
            text = text[qty_match.end():].strip()
        else:
            # Handle leading unicode fractions with no integer (e.g. "½ cup")
            uf_match = re.match(r"^([½⅓⅔¼¾⅛⅜⅝⅞]+)\s*", text)
            if uf_match and not a_match:
                quantity = _parse_quantity(uf_match.group(1))
                text = text[uf_match.end():].strip()

    # ── Step 2: Extract unit ─────────────────────────────────────────────────
    unit = "whole"
    words = text.split()
    if words:
        candidate = words[0].lower().rstrip("s.")  # try singular form too
        if words[0].lower() in UNIT_ALIASES:
            unit = UNIT_ALIASES[words[0].lower()]
            text = " ".join(words[1:])
        elif candidate in UNIT_ALIASES:
            unit = UNIT_ALIASES[candidate]
            text = " ".join(words[1:])

    # ── Step 3: Clean up the ingredient name ────────────────────────────────
    ingredient = _normalize_name(text)

    # If we ended up with an empty name (e.g. "salt and pepper" stripped to nothing,
    # or an orphaned number like "6"), fall back to the original minus quantity/unit,
    # but only if that also yields a real name.
    if not ingredient:
        ingredient = _normalize_name(original)

    return {
        "original": original,
        "quantity": round(quantity, 4),
        "unit": unit,
        "ingredient": ingredient,
    }


def split_and_parse(raw: str) -> list:
    """
    Parse a raw ingredient line, automatically splitting on 'and' when two
    distinct ingredients are combined on one line.

    Examples that get split:
        "Kosher salt and black pepper to taste"
            → [{"ingredient": "kosher salt", ...}, {"ingredient": "black pepper", ...}]
        "salt and pepper"
            → [{"ingredient": "salt", ...}, {"ingredient": "pepper", ...}]

    Examples that are NOT split (quantity present → single multi-part ingredient):
        "1 cup sugar and honey"      → kept as-is
        "pork and beans"             → both parts are valid → split
        "2 tbsp fish sauce and lime" → quantity at start → kept as-is

    Returns a list of dicts (usually 1, sometimes 2).
    Filters out any result whose ingredient name is empty (e.g. orphaned numbers).
    """
    raw = raw.strip()
    if not raw:
        return []

    and_pattern = re.compile(r"\s+and\s+", re.IGNORECASE)
    and_match = and_pattern.search(raw)

    if and_match:
        part_a = raw[: and_match.start()].strip()
        part_b = raw[and_match.end():].strip()

        # Only split if part_a does NOT start with a digit/fraction.
        # If it does, the "and" connects measurements or recipe steps, not two ingredients.
        a_starts_with_qty = bool(re.match(r'^[\d½⅓⅔¼¾⅛⅜⅝⅞]', part_a))

        if not a_starts_with_qty and part_a and part_b:
            parsed_a = parse_ingredient(part_a)
            parsed_b = parse_ingredient(part_b)

            # Only split if BOTH parts resolve to real, non-empty ingredient names
            if parsed_a["ingredient"] and parsed_b["ingredient"]:
                return [parsed_a, parsed_b]

    # Single ingredient path
    parsed = parse_ingredient(raw)
    if parsed["ingredient"]:
        return [parsed]
    return []


if __name__ == "__main__":
    # Quick self-test — run: python ingredient_parser.py
    test_cases = [
        # Standard cases
        "2 large carrots, peeled and diced",
        "1/2 cup all-purpose flour",
        "500g chicken breast, boneless",
        "1 1/2 tsp salt",
        "3 cloves garlic, minced",
        "½ onion, finely chopped",
        "1 can (400ml) diced tomatoes",
        "a pinch of cayenne pepper",
        "2-3 tablespoons olive oil",
        "1kg ground beef",
        "fresh parsley, for garnish",
        "4 cups chicken broth",
        "1 bunch kale, stems removed",
        # Tricky cases this parser is designed to fix
        "6-8 bone-in chicken thighs",
        "Kosher salt and black pepper to taste",
        "salt and pepper",
        "salt and pepper to taste",
        "fresh basil leaves, for serving",
        "6",                                    # orphaned number → should be empty/skipped
        "bone-in chicken thighs",              # second half of a split scrape
    ]

    print(f"\n{'ORIGINAL':<45} {'QTY':>6}  {'UNIT':<8}  {'INGREDIENT'}")
    print("-" * 85)

    for raw in test_cases:
        results = split_and_parse(raw)
        if not results:
            print(f"{raw[:44]:<45} {'—':>6}  {'—':<8}  (skipped — no ingredient found)")
        elif len(results) == 1:
            r = results[0]
            print(f"{r['original'][:44]:<45} {r['quantity']:>6.2f}  {r['unit']:<8}  {r['ingredient']}")
        else:
            for i, r in enumerate(results):
                label = r["original"][:44] if i == 0 else f"  └─ split {i+1}"
                print(f"{label:<45} {r['quantity']:>6.2f}  {r['unit']:<8}  {r['ingredient']}")
