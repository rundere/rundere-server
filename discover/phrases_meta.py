"""Canonical phrasebook metadata — a LEAF module (no discover imports).

`PHRASE_KEYS` is the ordered phrase set every language phrasebook renders. It
lives here, not in discover.resolvers.language, so the shared generator
(discover.phrasegen) can import it without dragging in the resolvers package and
re-creating the search ↔ resolvers import cycle. language.py re-exports it for
back-compat.
"""
from __future__ import annotations

# Canonical phrase set + English labels — the order rendered for every language.
PHRASE_KEYS: list[tuple[str, str]] = [
    ("hello", "Hello"), ("goodbye", "Goodbye"), ("please", "Please"),
    ("thanks", "Thank you"), ("yes", "Yes"), ("no", "No"),
    ("excuse", "Excuse me"), ("sorry", "Sorry"),
    ("speak_english", "Do you speak English?"),
    ("dont_understand", "I don't understand"), ("how_much", "How much is it?"),
    ("toilet", "Where is the toilet?"), ("help", "Help!"), ("cheers", "Cheers!"),
]

PHRASE_KEYSET = {k for k, _ in PHRASE_KEYS}
