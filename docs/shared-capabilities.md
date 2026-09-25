# Shared capabilities — build common, specialise at the edges

Reference implementation: `extract/directions_common.py`
(`build_directions_payload` + shared helpers), consumed by `extract/maps_url.py`
(Google) and `extract/apple_maps_url.py` (Apple).
Tests: `tests/test_extract.py` (the parity tests assert both providers behave
identically through the shared path).

Decisions: **D-89** (share, don't copy) and **D-96** (parity by construction).

---

## The guideline

A *capability* is a use case the application supports — capturing directions,
capturing a booking page, resolving a location, syncing to a target. A *channel*
or *provider* is one input source for that capability — Google Maps vs Apple Maps,
Gmail vs the bookmarklet, one geocoder vs another.

**Build the capability once, as a common function owned by the project. A channel
that needs something different is a *subsection* of that shared capability — a
thin parse/adapter layer or a parameter — never a parallel rewrite of the whole
thing.**

**The shared path is a ceiling-enabler, not a floor.** Sharing must not flatten a
richer input down to what the weakest channel can express — that is regression to
the mean, and it destroys value. The goal is *identical behaviour where the input
supports it*, and *maximum value where an input supports more*. The shared
function carries the common behaviour every channel needs; a channel whose input
carries more (Google's waypoints and embedded coordinates) still uses it to the
full — the shared path accommodates the richer input rather than discarding it.
Never level down to force uniformity.

Concretely:

- The **shared function owns the behaviour**: the output shape, the business
  rules, the defaults, the cross-cutting logic. There is exactly one place that
  builds a directions payload, and both providers call it.
- The **channel layer owns only what is genuinely channel-specific**: how to
  parse *that* provider's URL, what its mode signal looks like, which short-link
  form it uses. It hands normalised inputs to the shared function and gets a
  standard result back.
- A channel therefore cannot silently diverge. Add a directions capability to the
  shared builder and every provider gains it in the same commit; fix a bug there
  and every provider is fixed at once.

## Why

Cordoning a feature off into a channel-specific module feels faster for the first
channel and costs you on every one after: the second provider re-implements the
same logic slightly differently, the two drift, and a user hits a different
experience depending on which link they pasted. Parity maintained by *remembering
to mirror changes* rots. Parity maintained *by construction* — one shared
function — cannot.

## When you define a new capability or channel

Review the ask in the context of the whole application, not in isolation:

1. **Does a shared capability already cover this use case?** If yes, you are
   adding a *channel* to it. Extend the shared function (or add a parameter/hook),
   and write only the channel's parse/adapter layer. Do not start a new module
   that re-solves the common part.
2. **If it's genuinely new**, build the common function first — the part any
   future channel would reuse — then the channel-specific layer on top. Assume a
   second channel will come.
3. **Distinguish a format difference from parity debt.** It is fine — required,
   even — for one channel to carry a capability another's input cannot express:
   Google directions have waypoints and embedded coordinates; Apple's URL has
   neither. That is not divergence to fix; it is the shared function doing what
   each input supports, to the maximum it supports. Do not remove the richer
   behaviour to make the channels look alike. Divergence to fix is the opposite:
   when the *same* input could be handled the same way but isn't.
4. **Put the decision in the spec.** Record what became shared and what stayed
   channel-specific, so the next channel inherits the reasoning.

## Checklist for adding a channel to an existing capability

- [ ] The channel module only parses/normalises its own input; it does not
      rebuild the capability's output or re-state its rules.
- [ ] It calls the shared function for everything common.
- [ ] Any new common behaviour was added to the shared function, so existing
      channels get it too.
- [ ] Tests assert the new channel behaves identically to the others through the
      shared path (a parity test), plus its own input-parsing edge cases.
- [ ] Cross-cutting gates (e.g. `rules.py`) key off a capability-level set or
      type, not a single channel's literal name (see `DIRECTIONS_EXTRACTORS`).
