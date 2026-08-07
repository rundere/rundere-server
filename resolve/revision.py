"""Revision decisions — the create/update/no_change/conflict ladder (D-13).

Pure functions. `no_change` is the COMMON case: check-in reminders, boarding
passes and re-captures restate a booking already held. Identical hash => nothing
written, nothing reviewed. (The 7 identical debugging captures of one hotel
booking are exactly this.)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def content_hash(payload: dict) -> str:
    """sha256 of the normalised payload. Key order and whitespace must not
    change the hash, or every recapture would look like a change."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode()).hexdigest()


def changed_pointers(old: dict, new: dict, prefix: str = "") -> list[str]:
    """RFC 6901 JSON pointers to every differing leaf. Drives the review-UI
    diff and the field_locks collision check (RULE-15)."""
    out: list[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            token = str(key).replace("~", "~0").replace("/", "~1")
            out += changed_pointers(old.get(key), new.get(key), f"{prefix}/{token}")
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            o = old[i] if i < len(old) else None
            n = new[i] if i < len(new) else None
            out += changed_pointers(o, n, f"{prefix}/{i}")
    elif old != new:
        out.append(prefix or "/")
    return out


@dataclass(frozen=True)
class PriorItem:
    """What the resolver needs to know about the previously stored item that
    shares this canonical_key."""
    id: str
    content_hash: str
    document_timestamp: str | None = None
    revision: int = 1
    field_locks: tuple[str, ...] = ()
    has_target_record: bool = False     # already written to AirTrail/AdventureLog?


@dataclass(frozen=True)
class RevisionDecision:
    action: str                          # create|update|no_change|supersede|conflict|ignore
    content_hash: str = ""
    revision: int = 1
    prior_item_id: str | None = None
    prior_content_hash: str | None = None
    prior_document_timestamp: str | None = None
    changed_fields: tuple[str, ...] = ()
    field_locks: tuple[str, ...] = ()
    stale: bool = False
    review_required: bool = False

    def as_dict(self) -> dict:
        d = {"action": self.action, "content_hash": self.content_hash,
             "revision": self.revision, "changed_fields": list(self.changed_fields),
             "field_locks": list(self.field_locks), "stale": self.stale}
        if self.prior_item_id:
            d["prior_item_id"] = self.prior_item_id
        if self.prior_content_hash:
            d["prior_content_hash"] = self.prior_content_hash
        if self.prior_document_timestamp:
            d["prior_document_timestamp"] = self.prior_document_timestamp
        return d


def decide(payload: dict, prior: PriorItem | None,
           document_timestamp: str | None = None,
           prior_payload: dict | None = None) -> RevisionDecision:
    """The D-13 ladder, in the order the rules expect."""
    h = content_hash(payload)

    if prior is None:
        return RevisionDecision(action="create", content_hash=h, revision=1)

    # Identical content: the common case. Nothing written, nothing reviewed.
    if h == prior.content_hash:
        return RevisionDecision(
            action="no_change", content_hash=h, revision=prior.revision,
            prior_item_id=prior.id, prior_content_hash=prior.content_hash,
            prior_document_timestamp=prior.document_timestamp)

    changed = tuple(changed_pointers(prior_payload or {}, payload)) if prior_payload \
        else ()

    # Out-of-order delivery: an older document must never overwrite a newer one
    # (RULE-14). Common with airline schedule-change mail.
    stale = bool(document_timestamp and prior.document_timestamp
                 and str(document_timestamp) < str(prior.document_timestamp))
    if stale:
        return RevisionDecision(
            action="conflict", content_hash=h, revision=prior.revision,
            prior_item_id=prior.id, prior_content_hash=prior.content_hash,
            prior_document_timestamp=prior.document_timestamp,
            changed_fields=changed, field_locks=prior.field_locks,
            stale=True, review_required=True)          # RULE-15b

    # A human edited these fields in the review UI; never silently overwrite
    # them (RULE-15).
    clash = tuple(sorted(set(prior.field_locks) & set(changed)))
    if clash:
        return RevisionDecision(
            action="conflict", content_hash=h, revision=prior.revision,
            prior_item_id=prior.id, prior_content_hash=prior.content_hash,
            prior_document_timestamp=prior.document_timestamp,
            changed_fields=changed, field_locks=prior.field_locks,
            review_required=True)

    # A cancellation supersedes rather than updates (D-13).
    if payload.get("booking_status") == "cancelled":
        return RevisionDecision(
            action="supersede", content_hash=h, revision=prior.revision + 1,
            prior_item_id=prior.id, prior_content_hash=prior.content_hash,
            prior_document_timestamp=prior.document_timestamp,
            changed_fields=changed, field_locks=prior.field_locks)

    # RULE-18: an update with no changed fields should have been no_change.
    # Hashes differ, so if the differ found nothing the payload shape changed
    # in a way worth a human look rather than a silent write.
    if prior_payload is not None and not changed:
        return RevisionDecision(
            action="no_change", content_hash=h, revision=prior.revision,
            prior_item_id=prior.id, prior_content_hash=prior.content_hash,
            prior_document_timestamp=prior.document_timestamp)

    return RevisionDecision(
        action="update", content_hash=h, revision=prior.revision + 1,
        prior_item_id=prior.id, prior_content_hash=prior.content_hash,
        prior_document_timestamp=prior.document_timestamp,
        changed_fields=changed or ("/",), field_locks=prior.field_locks)
