"""Yonder 3.1 — Planning: the Packing Plan (SPEC-3.1-planning.md).

Yonder's second trip-prep pillar. Discovery scouts ("what is it like there?");
Planning packs ("what do I bring?"). Planning is an AGGREGATOR that sits downstream
of Discovery and the trip: it derives the trip's context first-party, scores
curated archetype templates, runs deterministic modifier/scaling passes, and
produces one merged, right-sized, bag-assigned Packing Plan you review and correct.

`planning/data/` holds the curated reference datasets (the "template database");
`planning/research/` keeps the owner's source research these were distilled from.
"""
