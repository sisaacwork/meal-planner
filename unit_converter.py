"""
unit_converter.py
-----------------
Converts US/imperial recipe units to metric for Canadian grocery shopping.

Recipes often say "2 cups" or "1 lb" — but Canadian store labels say "500 ml"
and "450 g". This module bridges that gap so your shopping list matches
what's actually on the shelf.
"""

# ── Conversion tables ─────────────────────────────────────────────────────────

# Everything converts to ml first, then we smart-format the result
VOLUME_TO_ML: dict[str, float] = {
    # US units
    "cup":   236.588,
    "tbsp":  14.787,
    "tsp":   4.929,
    "fl oz": 29.574,
    # Already metric — include so we can still smart-format them
    "ml":    1.0,
    "litre": 1000.0,
    "l":     1000.0,
}

# Everything converts to grams first, then we smart-format the result
WEIGHT_TO_G: dict[str, float] = {
    # US/imperial units
    "lb":  453.592,
    "lbs": 453.592,
    "oz":  28.3495,
    # Already metric — include so we can still smart-format them
    "g":   1.0,
    "kg":  1000.0,
}

# Units that represent a countable thing — can't convert, keep as-is
COUNT_UNITS = {
    "whole", "clove", "cloves", "can", "cans", "pkg", "package",
    "slice", "slices", "bunch", "bunches", "handful", "handfuls",
    "pinch", "pinches", "dash", "dashes", "sprig", "sprigs",
}

VOLUME_UNITS = set(VOLUME_TO_ML.keys())
WEIGHT_UNITS = set(WEIGHT_TO_G.keys())
ALL_KNOWN_UNITS = VOLUME_UNITS | WEIGHT_UNITS | COUNT_UNITS


def convert_to_metric(quantity: float, unit: str) -> tuple[float, str]:
    """
    Convert a quantity + unit pair to its metric equivalent.

    Returns (converted_quantity, metric_unit).
    - Volume → ml if < 1000 ml, otherwise L
    - Weight → g if < 1000 g, otherwise kg
    - Count / unknown units → returned unchanged

    Examples:
        convert_to_metric(2, "cup")   → (473.0, "ml")
        convert_to_metric(6, "cup")   → (1.42, "L")
        convert_to_metric(1, "lb")    → (454.0, "g")
        convert_to_metric(3, "lb")    → (1.36, "kg")
        convert_to_metric(500, "g")   → (500.0, "g")   # already metric
        convert_to_metric(2, "whole") → (2, "whole")    # can't convert
    """
    unit_lower = unit.lower().strip()

    if unit_lower in VOLUME_TO_ML:
        ml = quantity * VOLUME_TO_ML[unit_lower]
        if ml >= 1000:
            return round(ml / 1000, 2), "L"
        return round(ml), "ml"

    if unit_lower in WEIGHT_TO_G:
        g = quantity * WEIGHT_TO_G[unit_lower]
        if g >= 1000:
            return round(g / 1000, 2), "kg"
        return round(g), "g"

    # Count unit or something unrecognised — return as-is
    return quantity, unit


def normalise_to_base(quantity: float, unit: str) -> tuple[float, str] | None:
    """
    Convert to a base metric unit (ml or g) for aggregation purposes.
    Returns (base_quantity, base_unit) or None if the unit can't be converted.

    This is used by the shopping list consolidator so that, e.g.,
    "2 cups chicken broth" + "500 ml chicken broth" add up correctly.
    """
    unit_lower = unit.lower().strip()
    if unit_lower in VOLUME_TO_ML:
        return quantity * VOLUME_TO_ML[unit_lower], "ml"
    if unit_lower in WEIGHT_TO_G:
        return quantity * WEIGHT_TO_G[unit_lower], "g"
    return None


def same_dimension(unit_a: str, unit_b: str) -> bool:
    """Return True if both units measure the same thing (both volume or both weight)."""
    a = unit_a.lower().strip()
    b = unit_b.lower().strip()
    return (a in VOLUME_UNITS and b in VOLUME_UNITS) or \
           (a in WEIGHT_UNITS and b in WEIGHT_UNITS)


def format_metric(quantity: float, unit: str) -> str:
    """
    Format a metric quantity nicely for display.
    Whole numbers drop the decimal; decimals strip trailing zeros.
    e.g. 1.0 kg → "1 kg",  1.50 L → "1.5 L",  1.13 kg → "1.13 kg"
    """
    if quantity == int(quantity):
        return f"{int(quantity)} {unit}"
    return f"{float(f'{quantity:.2f}'):g} {unit}"


if __name__ == "__main__":
    # Quick self-test
    tests = [
        (2,    "cup",   "473 ml"),
        (6,    "cup",   "1.42 L"),
        (0.5,  "cup",   "118 ml"),
        (1,    "tbsp",  "15 ml"),
        (1,    "tsp",   "5 ml"),
        (1,    "lb",    "454 g"),
        (2.5,  "lb",    "1.13 kg"),
        (4,    "oz",    "113 g"),
        (500,  "g",     "500 g"),
        (1,    "kg",    "1 kg"),
        (250,  "ml",    "250 ml"),
        (1.5,  "litre", "1.5 L"),

        (3,    "whole", "3 whole"),
        (2,    "clove", "2 clove"),
    ]

    print(f"{'INPUT':<20} {'EXPECTED':<12} {'GOT':<12} {'PASS?'}")
    print("-" * 58)
    for qty, unit, expected in tests:
        result_qty, result_unit = convert_to_metric(qty, unit)
        got = format_metric(result_qty, result_unit)
        match = "✅" if got == expected else "❌"
        print(f"{f'{qty} {unit}':<20} {expected:<12} {got:<12} {match}")
