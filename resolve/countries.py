"""Country display names from CLDR via Babel (SPEC-8 D-148, YONDER-190).

Trip names are minted from a country ("Aruba 2031"), and used to come
from AdventureLog's country list. CLDR is already bundled with Babel (the
currency code uses it too), so names no longer depend on an integration.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def country_names() -> dict[str, str]:
    """{ISO alpha-2: English name}, e.g. {"AW": "Aruba"}."""
    try:
        from babel import Locale
        terr = Locale.parse("en").territories
    except Exception:                       # Babel missing data: raw codes it is
        return {}
    return {code: name for code, name in terr.items()
            if len(code) == 2 and code.isalpha()}
