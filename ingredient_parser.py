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

# Unicode fractions and their decimal equivalents
UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3,
    "¼": 0.25, "¾": 0.75,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}


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

    # Range: "2-3" → take the average
    rng = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", text)
    if rng:
        return (float(rng.group(1)) + float(rng.group(2))) / 2

    try:
        return float(text)
    except ValueError:
        return 1.0


def _normalize_name(text: str) -> str:
    """
    Strip prep words, punctuation, and parenthetical notes from an ingredient name.
    e.g. "large carrots, peeled (about 200g)" → "carrots"
    """
    # Remove parenthetical notes
    text = re.sub(r"\(.*?\)", "", text)

    # Remove everything after a comma
    text = text.split(",")[0]

    # Split into words, remove prep words and linking words, rejoin
    SKIP = PREP_WORDS | {"of", "for", "to", "and", "or", "with", "the", "a", "an"}
    words = text.lower().split()
    cleaned = [w.strip(".,;:") for w in words if w.strip(".,;:") not in SKIP]

    return " ".join(cleaned).strip()


def parse_ingredient(raw: str) -> dict:
    """
    Parse a raw ingredient string into structured components.

    Returns a dict with:
      - original  : the unchanged input string
      - quantity  : float (e.g. 2.0, 0.5)
      - unit      : canonical unit string, or "whole" if no unit found
      - ingredient: cleaned ingredient name
    """
    original = raw.strip()
    text = original

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

    # Handle ranges like "2-3" — take the lower bound (conservative shopping)
    range_match = re.match(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s+", text)
    if range_match and not a_match:
        quantity = float(range_match.group(1))  # use the lower bound
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

    # If we ended up with an empty name (e.g. "salt"), fall back to the original minus quantity
    if not ingredient:
        ingredient = _normalize_name(original)

    return {
        "original": original,
        "quantity": round(quantity, 4),
        "unit": unit,
        "ingredient": ingredient,
    }


if __name__ == "__main__":
    # Quick self-test — run: python ingredient_parser.py
    test_cases = [
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
    ]

    print(f"{'ORIGINAL':<40} {'QTY':>6}  {'UNIT':<8}  {'INGREDIENT'}")
    print("-" * 80)
    for raw in test_cases:
        result = parse_ingredient(raw)
        print(
            f"{result['original'][:39]:<40} "
            f"{result['quantity']:>6.2f}  "
            f"{result['unit']:<8}  "
            f"{result['ingredient']}"
        )
