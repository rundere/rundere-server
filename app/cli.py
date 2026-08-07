"""Operational CLI. Usage (inside the container or a dev venv):

    python -m app.cli create-key <client-name>     # prints the key ONCE
    python -m app.cli revoke-key <client-name>
    python -m app.cli list-keys
    python -m app.cli process [--no-llm] [--limit N]   # extract received batches
    python -m app.cli import-home <file.json>          # bootstrap home_profiles;
                                                      # manage them in /admin after
    python -m app.cli resolve [--no-adventurelog] [--limit N] [--redo]
                                                      # keys, dedup, home,
                                                      # collections, rules.
                                                      # --redo re-resolves items
                                                      # a human hasn't acted on
    python -m app.cli commit [--limit N] [--execute]   # write APPROVED items to
                                                      # AirTrail + AdventureLog.
                                                      # DRY-RUN by default; shows
                                                      # exact bodies. --execute
                                                      # performs the real writes.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app import auth
from app.db import SessionLocal
from app.models import ApiKey


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    with SessionLocal() as session:
        if cmd == "create-key":
            (client,) = args
            existing = session.query(ApiKey).filter_by(client_name=client).one_or_none()
            key = auth.generate_key()
            if existing and existing.revoked_at is None:
                print(f"client {client!r} already has an ACTIVE key — revoke it first")
                return 1
            if existing:
                # Rotation: client_name is unique, so a revoked row permanently
                # owns the name. Reuse the row with fresh key material rather
                # than trying (and failing) to insert a duplicate.
                existing.key_hash = auth.hash_key(key)
                existing.created_at = datetime.now(timezone.utc)
                existing.revoked_at = None
                existing.last_used_at = None
            else:
                session.add(ApiKey(client_name=client, key_hash=auth.hash_key(key)))
            session.commit()
            print(f"API key for {client!r} (shown once, store it now):\n{key}")
        elif cmd == "revoke-key":
            (client,) = args
            row = session.query(ApiKey).filter_by(client_name=client).one_or_none()
            if not row:
                print(f"no key for client {client!r}")
                return 1
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
            print(f"revoked key for {client!r}")
        elif cmd == "list-keys":
            for r in session.query(ApiKey).all():
                state = "REVOKED" if r.revoked_at else "active"
                print(f"{r.client_name:30} {state:8} last_used={r.last_used_at}")
        elif cmd == "import-home":
            # Home profiles are per-deployment personal config and are MANAGED
            # IN THE ADMIN UI (/admin -> Home Profiles). This importer exists
            # only to bootstrap a new stack from a local file — nothing about
            # anyone's home is hardcoded in this repo (D-21: it is shared).
            import json as _json
            from app.models import HomeProfile
            if not args:
                print("usage: python -m app.cli import-home <path.json>\n"
                      "see docs/home-profiles.example.json for the shape")
                return 2
            rows = _json.loads(open(args[0]).read())
            if not isinstance(rows, list):
                print("file must contain a JSON array of profile objects")
                return 1
            allowed = {"kind", "label", "iata", "lat", "lon", "radius_km",
                       "country", "effective_from", "effective_to"}
            existing = {p.label for p in session.query(HomeProfile).all()}
            added = 0
            for r in rows:
                unknown = set(r) - allowed
                if unknown:
                    print(f"  skipped {r.get('label')!r}: unknown field(s) {sorted(unknown)}")
                    continue
                if r.get("label") in existing:
                    print(f"  skipped {r['label']!r}: already present (edit it in /admin)")
                    continue
                session.add(HomeProfile(**r))
                added += 1
            session.commit()
            print(f"imported {added} home profile(s). Manage them at /admin -> Home Profiles.")
            for p in session.query(HomeProfile).order_by(HomeProfile.effective_from).all():
                span = f"{p.effective_from or 'start'} .. {p.effective_to or 'current'}"
                print(f"  [{p.kind:13}] {p.label:35} {span}  r={p.radius_km}km")
        elif cmd == "process":
            from app.config import settings
            from app.processing import process_pending
            llm = None
            if "--no-llm" not in args:
                from extract.llm import OllamaExtractor
                llm = OllamaExtractor(settings.ollama_url, settings.ollama_model,
                                      settings.llm_timeout_seconds)
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            summary = process_pending(session, llm, limit=limit)
            print(f"extracted={summary['extracted']} "
                  f"needs_review={summary['needs_review']}"
                  + (" (llm disabled)" if llm is None else f" (model={llm.model})"))
        elif cmd == "resolve":
            from app.config import settings
            from app.resolving import resolve_pending
            client = None
            if "--no-adventurelog" not in args:
                from adapters.targets.adventurelog import AdventureLogClient
                if not settings.adventurelog_api_key:
                    print("TX_ADVENTURELOG_API_KEY is not set — either set it or "
                          "pass --no-adventurelog (every item will then defer, "
                          "since no existing collection can be matched)")
                    return 1
                client = AdventureLogClient(settings.adventurelog_url,
                                            settings.adventurelog_api_key)
            # AirTrail supplies airport reference data (country/coords/tz);
            # Photon locates everything else (hotels, venues). Both optional —
            # without them, places stay unlocated and more items go to review.
            airtrail = None
            if settings.airtrail_token and "--no-airtrail" not in args:
                from adapters.targets.airtrail import AirTrailClient
                airtrail = AirTrailClient(settings.airtrail_url, settings.airtrail_token)
            geocoder = None
            if "--no-geocode" not in args:
                from resolve.geocode import PhotonGeocoder
                geocoder = PhotonGeocoder(settings.photon_url)
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            summary = resolve_pending(session, client, limit=limit,
                                      airtrail=airtrail, geocoder=geocoder,
                                      redo="--redo" in args)
            print("resolve: " + (", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
                                 or "nothing pending"))
        elif cmd == "commit":
            from app.committing import commit_pending
            from app.config import settings
            from app.resolving import load_identities
            execute = "--execute" in args
            adventurelog = airtrail = None
            if settings.adventurelog_api_key:
                from adapters.targets.adventurelog import AdventureLogClient
                adventurelog = AdventureLogClient(settings.adventurelog_url,
                                                  settings.adventurelog_api_key)
            if settings.airtrail_token:
                from adapters.targets.airtrail import AirTrailClient
                airtrail = AirTrailClient(settings.airtrail_url, settings.airtrail_token)
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            summary = commit_pending(session, adventurelog, airtrail,
                                     identities=load_identities(session),
                                     dry_run=not execute, limit=limit)
            mode = "EXECUTED" if execute else \
                "DRY-RUN — no writes; pass --execute to commit"
            print(f"commit [{mode}]: items={summary['items']} "
                  f"ok={summary['writes_ok']} failed={summary['writes_failed']} "
                  f"skipped={summary['skipped']}")
            for plan in summary.get("plans", []):
                print(f"\n  • {plan['headline']}")
                for r in plan["results"]:
                    tag = (r.get("skipped") or ("PLAN" if r.get("dry_run")
                           else ("ok" if r.get("ok") else "FAIL")))
                    line = f"      [{tag}] {r['target']}"
                    if r.get("error"):
                        line += f" — {r['error']}"
                    print(line)
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
