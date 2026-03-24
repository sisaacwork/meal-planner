"""
price_filter.py
---------------
Shared relevance-filtering logic used by both flipp_client.py and
instacart_client.py to avoid a circular import between the two.
"""

import re

_PROCESSED_FORMS = {
    "pesto", "sauce", "paste", "spread", "jam", "jelly", "extract",
    "ketchup", "salsa", "dip", "powder", "flakes", "seasoning",
    "blend", "mix", "marinade", "syrup", "concentrate",
    "puree", "purée", "relish", "chutney", "coulis",
    "soup", "stew", "curry", "gravy",
    "pickle", "pickled", "fermented",
    "flavour", "flavor", "infused",
    "oil",
    "vinegar",
    "butter",
    "cream",
    "juice",
}

_ING_STOP_WORDS = {
    "fresh", "dried", "frozen", "canned", "whole", "ground",
    "chopped", "sliced", "diced", "minced", "cooked", "raw",
    "organic", "extra", "virgin", "large", "small", "medium",
    "boneless", "skinless", "free", "range",
    "a", "an", "the", "of", "and", "or", "for", "with", "to",
}


def _key_words(text: str) -> list:
    return [
        w.lower().strip(".,;:-()[]\"'")
        for w in text.split()
        if w.lower().strip(".,;:-()[]\"'") not in _ING_STOP_WORDS
        and len(w.strip(".,;:-()[]\"'")) > 1
        and not re.fullmatch(r"[\d.,]+(?:g|kg|ml|l|oz|lb|pk|ct)?", w.lower().strip(".,;:-()[]"))
    ]


def _word_in_text(needle: str, text: str) -> bool:
    text = text.lower()
    stem = needle.rstrip("s") if len(needle) > 3 else needle
    for form in {needle, stem, stem + "s", stem + "es"}:
        if re.search(r"\b" + re.escape(form) + r"\b", text):
            return True
    return False


def is_relevant_result(ingredient: str, product_name: str) -> bool:
    """
    Return True only if the product is a plausible match for the ingredient.

    Rule 1: every key word from the ingredient must appear as a whole word in
            the product name.
    Rule 2: the product name must not contain a processed-form word (pesto,
            powder, sauce…) that isn't already in the ingredient name.
    """
    ing_words = _key_words(ingredient)
    if not ing_words:
        return True

    for word in ing_words:
        if not _word_in_text(word, product_name):
            return False

    ing_lower = ingredient.lower()
    product_lower = product_name.lower()
    for form in _PROCESSED_FORMS:
        if (re.search(r"\b" + re.escape(form) + r"\b", product_lower)
                and not re.search(r"\b" + re.escape(form) + r"\b", ing_lower)):
            return False

    return True
