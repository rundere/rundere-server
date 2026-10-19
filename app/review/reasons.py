"""Turn machine reason codes into sentences a person can act on.

review.reasons is a closed enum (rules.REVIEW_REASON_FOR_RULE maps rule ->
code). The queue is unusable if it shows codes: "RULE-04 precision=date" tells
you nothing about what to DO. Each sentence therefore names the problem in the
document's terms and states the action that clears it.
"""
from __future__ import annotations

# code -> (headline sentence, what unblocks it)
REASON_TEXT: dict[str, tuple[str, str]] = {
    "missing_time": (
        "The document never stated a time for this.",
        "Add the time, or log it as a date-only entry."),
    "missing_timezone": (
        "The timezone on this can’t be used as written.",
        "Set a standard timezone."),
    "missing_travel_date": (
        "This capture has no travel date, and the source doesn’t carry one.",
        "Add the date you travelled."),
    "missing_capture_context": (
        "Some details had to come from you, and weren’t supplied.",
        "Fill in the missing details."),
    "missing_passenger_list": (
        "The document didn’t list passengers, so this was assumed to be you.",
        "Confirm who travelled."),
    "no_collection_match": (
        "No trip covers these dates yet.",
        "Pick a trip, or wait for a flight to create one."),
    "ambiguous_collection": (
        "More than one trip could hold this.",
        "Choose which trip it belongs to."),
    "collection_not_selected": (
        "This needs you to say which trip it belongs to.",
        "Choose a trip."),
    "conflicting_version": (
        "This disagrees with what’s already stored for the same booking.",
        "Check which version is right."),
    "stale_document": (
        "This document is older than the version already stored.",
        "Confirm before letting it overwrite newer details."),
    "locked_field_conflict": (
        "This would overwrite details you corrected by hand.",
        "Keep your version, or accept the new one."),
    "cancellation_no_match": (
        "This is a cancellation, but no matching booking was found.",
        "Link it to the booking it cancels, or ignore it."),
    "segment_count_mismatch": (
        "The number of segments doesn’t match what the document declared.",
        "Check whether a leg is missing."),
    "geocode_failed": (
        "This place couldn’t be located.",
        "Add an address or city."),
    "unknown_type": (
        "It isn’t clear what kind of booking this is.",
        "Set the type."),
    "missing_airport_code": (
        "This flight’s document named the airports but never printed their codes.",
        "Enter the 3-letter airport code (e.g. YYZ) for departure and arrival."),
    "low_confidence": (
        "Something about this doesn’t look right.",
        "Check the details against the original."),
    "home_only_movement": (
        "This never left your home area, so it isn’t a trip.",
        "Ignore it, or adjust your home settings."),
    "route_unparsed": (
        "The route couldn’t be read from this.",
        "Add the route details."),
    "waypoint_ambiguous": (
        "The stops on this route are out of order or unclear.",
        "Check the stop order."),
    "partial_commit": (
        "Part of this was logged and part wasn’t.",
        "Retry the parts that failed."),
    "commit_failed": (
        "Logging this failed.",
        "Check the error and retry."),
}

_FALLBACK = ("This needs a look before it can be logged.",
             "Check the details against the original.")


def headline(codes: list[str] | None) -> str:
    if not codes:
        return ""
    return REASON_TEXT.get(codes[0], _FALLBACK)[0]


def action_hint(codes: list[str] | None) -> str:
    if not codes:
        return ""
    return REASON_TEXT.get(codes[0], _FALLBACK)[1]
