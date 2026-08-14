"""Yonder 3.0 — Discovery: the in-house trip research pipeline (SPEC-3-discovery.md).

Sibling to `resolve/` (which serves the capture→commit pipeline). `discover/`
serves the trip→briefing pipeline: derive a trip's locations first-party from its
own activities, research them locally (SearXNG + Ollama + Photon), and hold the
findings on Yonder for review before pushing to AdventureLog.
"""
