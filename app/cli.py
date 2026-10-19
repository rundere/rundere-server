"""Operational CLI. Usage (inside the container or a dev venv):

    python -m app.cli create-key <client-name> [--user <username>]  # prints the key ONCE
                                                      # --user scopes captures to that user (4.0)
    python -m app.cli revoke-key <client-name>
    python -m app.cli list-keys
    python -m app.cli create-user <username> [--admin] [--password PW]  # 4.0 auth
    python -m app.cli set-password <username> [--password PW]
    python -m app.cli set-email <username> <email>     # blank "" clears it
    python -m app.cli make-admin <username> [--revoke]
    python -m app.cli link-traveler <username> <traveler_id>
    python -m app.cli list-users
    python -m app.cli oauth-client create <name> --redirect <uri> [--redirect <uri>…] [--confidential]
                                                      # pre-register an OAuth client (e.g. for
                                                      # claude.ai's advanced settings); prints
                                                      # the id (+ secret, once)
    python -m app.cli oauth-client list
    python -m app.cli process [--no-llm] [--limit N]   # extract received batches
    python -m app.cli retention [--dry-run] [--no-mailbox]   # D-196: the daily sweep, by hand
    python -m app.cli import-home <file.json>          # bootstrap home_profiles;
                                                      # manage them in /admin after
    python -m app.cli resolve [--no-adventurelog] [--no-airtrail] [--limit N] [--redo]
                                                      # keys, dedup, home,
                                                      # collections, rules, each
                                                      # capture with its owner's
                                                      # connections (D-150).
                                                      # --redo re-resolves items
                                                      # a human hasn't acted on
    python -m app.cli locate-activities [--trip ID] [--redo]
                                                      # store coordinates (and road
                                                      # trip route lines) for timeline
                                                      # activities saved before 4.2.x;
                                                      # --redo re-locates every one
    python -m app.cli purge-orphans [--execute]         # remove data left behind by
                                                      # trips deleted before 4.5 (Discover,
                                                      # packing, timeline); unlink captures
                                                      # and journal entries. DRY-RUN by default.
    python -m app.cli commit [--limit N] [--execute]   # write APPROVED items to
                                                      # AirTrail + AdventureLog.
                                                      # DRY-RUN by default; shows
                                                      # exact bodies. --execute
                                                      # performs the real writes,
                                                      # to each owner's connections.
    python -m app.cli connections                      # who has which AirTrail /
                                                      # AdventureLog connection (no secrets)
    python -m app.cli secrets-rotate                   # re-encrypt every stored credential
                                                      # with the newest TX_SECRETS_KEYS key (D-151)
    python -m app.cli demo-seed [--replace]            # D-198: write the demo server's sample
                                                      # trips (--replace: after a release
                                                      # changed the seed). Demo servers only.
    python -m app.cli demo-roll                        # move the sample trips' dates to today
                                                      # (the nightly job, by hand)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app import auth
from app.db import SessionLocal
from app.models import ApiKey


def _opt(args: list[str], name: str, default=None):
    return args[args.index(name) + 1] if name in args else default


def _user_cmd(session, cmd: str, args: list[str]) -> int:
    """Yonder 4.0 auth (SPEC-4 Phase 1a) user administration.

        create-user <username> [--admin] [--password PW] [--email E] [--traveler ID]
        set-password <username> [--password PW]
        set-email   <username> <email>   ("" clears)
        make-admin  <username> [--revoke]
        link-traveler <username> <traveler_id>
        list-users
    """
    import getpass

    from app import authn
    from app.models import TravelerIdentity, User

    def _read_password(supplied):
        if supplied:
            return supplied
        p1 = getpass.getpass("password: ")
        if p1 != getpass.getpass("confirm : "):
            print("passwords did not match")
            return None
        return p1

    if cmd == "list-users":
        rows = session.query(User).order_by(User.username).all()
        if not rows:
            print("no users yet — create one with: create-user <username> --admin")
        for u in rows:
            flags = ",".join(f for f, on in
                             (("admin", u.is_admin), ("inactive", not u.is_active)) if on)
            print(f"{u.username:24} {u.auth_source:6} email={u.email or '—':28} "
                  f"traveler={u.traveler_id} "
                  f"[{flags or 'user'}] last_login={u.last_login_at}")
        return 0

    if not args:
        print(_user_cmd.__doc__)
        return 2
    username = args[0]
    user = session.query(User).filter_by(username=username).one_or_none()

    if cmd == "create-user":
        if user is not None:
            print(f"user {username!r} already exists (use set-password / make-admin)")
            return 1
        pw = _read_password(_opt(args, "--password"))
        if pw is None:
            return 1
        traveler_id = _opt(args, "--traveler")
        user = User(username=username, email=_opt(args, "--email"),
                    password_hash=authn.hash_password(pw), auth_source="local",
                    is_admin="--admin" in args,
                    traveler_id=int(traveler_id) if traveler_id else None)
        session.add(user)
        session.commit()
        print(f"created user {username!r}"
              + (" (admin)" if user.is_admin else "")
              + (f", linked to traveler {traveler_id}" if traveler_id else ""))
        return 0

    if user is None:
        print(f"no user {username!r}")
        return 1

    if cmd == "set-password":
        pw = _read_password(_opt(args, "--password"))
        if pw is None:
            return 1
        user.password_hash = authn.hash_password(pw)
        authn.end_sessions(session, user)   # invalidate existing sessions on a reset
        from app.models import AccountEvent   # audit: the operator, via the CLI
        session.add(AccountEvent(user_id=user.id, actor_id=None, action="password_set",
                                 detail="cli"))
        session.commit()
        print(f"password updated for {username!r} (existing sessions signed out)")
        return 0

    if cmd == "set-email":
        if len(args) < 2:
            print('usage: set-email <username> <email>   (use "" to clear)')
            return 2
        error = authn.set_user_email(session, user, args[1])
        if error:
            print(error)
            return 1
        from app.models import AccountEvent
        session.add(AccountEvent(user_id=user.id, actor_id=None, action="email_set",
                                 detail="cli"))
        session.commit()
        print(f"email for {username!r} " + (f"set to {user.email}" if user.email else "cleared"))
        return 0

    if cmd == "make-admin":
        user.is_admin = "--revoke" not in args
        session.commit()
        print(f"{username!r} is now {'a regular user' if '--revoke' in args else 'an admin'}")
        return 0

    if cmd == "link-traveler":
        if len(args) < 2:
            print("usage: link-traveler <username> <traveler_id>")
            return 2
        tid = int(args[1])
        if session.get(TravelerIdentity, tid) is None:
            print(f"no traveler with id {tid} (see /admin -> Travellers)")
            return 1
        user.traveler_id = tid
        session.commit()
        print(f"linked {username!r} to traveler {tid}")
        return 0

    return 2


def _oauth_client_cmd(session, args: list[str]) -> int:
    """YONDER-107: pre-registered OAuth clients (claude.ai "advanced settings"
    takes a client id + optional secret; DCR needs none of this)."""
    from app import oauth
    from app.models import OAuthClient
    sub = args[0] if args else ""
    if sub == "list":
        for c in session.query(OAuthClient).order_by(OAuthClient.created_at).all():
            kind = "confidential" if c.secret_hash else "public"
            print(f"{c.id:30} {kind:12} {c.source:4} {c.name!r} {c.redirect_uris}")
        return 0
    if sub != "create" or len(args) < 2 or args[1].startswith("--"):
        print("usage: python -m app.cli oauth-client create <name> --redirect <uri> "
              "[--redirect <uri>…] [--confidential]\n       python -m app.cli oauth-client list")
        return 2
    name = args[1]
    redirects = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--redirect"]
    try:
        client, secret = oauth.register_client(
            session, name=name, redirect_uris=redirects,
            confidential="--confidential" in args, source="cli")
    except oauth.OAuthError as e:
        print(f"error: {e.description or e.error}")
        return 1
    print(f"OAuth client {client.name!r}\n  client_id:     {client.id}")
    if secret:
        print(f"  client_secret: {secret}\n  (shown once — store it now)")
    print(f"  redirect_uris: {', '.join(client.redirect_uris)}")
    return 0


def _demo_cmd(session, cmd: str, args: list[str]) -> int:
    """D-198: the demo server's sample data. Refuses anywhere but a demo server,
    behind the same startup checks the server runs."""
    from app import demo, demo_seed
    if not demo.enabled():
        print("not a demo server (TX_DEMO_MODE is off); refusing to write sample data")
        return 1
    try:
        demo.check_startup(session)
    except demo.DemoMisconfigured as e:
        print(e)
        return 1
    if cmd == "demo-seed":
        out = demo_seed.seed(session, replace="--replace" in args)
        print("seeded: " + ", ".join(out["trips"]) if out["seeded"] else out["reason"])
    else:
        out = demo_seed.roll(session)
        print(f"moved the sample trips {out['rolled']:+d} day(s)" if out["rolled"]
              else "already current")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    with SessionLocal() as session:
        if cmd == "create-key":
            client = args[0]
            # Phase 2 (D-120): --user <name> scopes the key to a user, so captures
            # it submits are owned by them (each member runs their own key).
            user_id = None
            if "--user" in args:
                from app.models import User
                uname = args[args.index("--user") + 1]
                u = session.query(User).filter_by(username=uname).one_or_none()
                if u is None:
                    print(f"no user {uname!r} (create it first, or omit --user for an open key)")
                    return 1
                user_id = u.id
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
                existing.user_id = user_id
            else:
                session.add(ApiKey(client_name=client, key_hash=auth.hash_key(key),
                                   user_id=user_id))
            session.commit()
            scoped = f" (scoped to user {args[args.index('--user') + 1]!r})" if user_id else " (open/unscoped)"
            print(f"API key for {client!r}{scoped} (shown once, store it now):\n{key}")
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
            from app.models import User
            for r in session.query(ApiKey).all():
                state = "REVOKED" if r.revoked_at else "active"
                who = session.get(User, r.user_id).username if r.user_id else "—(open)"
                print(f"{r.client_name:30} {state:8} user={who:16} last_used={r.last_used_at}")
        elif cmd in ("create-user", "set-password", "set-email", "make-admin", "list-users",
                     "link-traveler"):
            return _user_cmd(session, cmd, args)
        elif cmd == "oauth-client":
            return _oauth_client_cmd(session, args)
        elif cmd in ("demo-seed", "demo-roll"):
            return _demo_cmd(session, cmd, args)
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
        elif cmd == "retention":
            # SPEC-8 D-196: what the daily sweep removes; --dry-run only counts.
            from app import retention
            report = retention.run(SessionLocal, dry_run="--dry-run" in args,
                                   mailbox="--no-mailbox" not in args)
            print(("dry run: " if "--dry-run" in args else "") +
                  " ".join(f"{k}={v}" for k, v in report.summary().items()))
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
            from app.integrations import ClientCache
            from app.resolving import resolve_pending
            # Each capture resolves with its owner's connections (D-150); the
            # flags leave a system out for everyone. Photon locates hotels and
            # venues; airports come from Run·dere's own table (D-147).
            drop = frozenset(k for k, flag in (("adventurelog", "--no-adventurelog"),
                                               ("airtrail", "--no-airtrail")) if flag in args)
            geocoder = None
            if "--no-geocode" not in args:
                from resolve.geocode import PhotonGeocoder
                geocoder = PhotonGeocoder(settings.photon_url)
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            summary = resolve_pending(session, limit=limit, geocoder=geocoder,
                                      redo="--redo" in args,
                                      clients_for=ClientCache(session, drop))
            print("resolve: " + (", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
                                 or "nothing pending"))
        elif cmd == "locate-activities":
            from app.trips.geo import backfill
            counts = backfill(session, trip_id=_opt(args, "--trip"),
                              redo="--redo" in args, progress=print)
            print("locate-activities: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        elif cmd == "commit":
            from app.committing import commit_pending
            from app.integrations import ClientCache
            from app.resolving import load_identities
            execute = "--execute" in args
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 0
            summary = commit_pending(session, identities=load_identities(session),
                                     dry_run=not execute, limit=limit,
                                     clients_for=ClientCache(session))
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
        elif cmd == "connections":
            from app import integrations
            rows = integrations.admin_overview(session)
            for r in rows:
                state = ("off" if not r["enabled"] else
                         {True: "ok", False: "failing", None: "untested"}[r["ok"]])
                print(f"  {r['user']:24} {r['kind']:13} {r['host']:32} {state}")
            if not rows:
                print("  no connections")
            for name in integrations.deprecated_env():
                print(f"  deprecated: {name} is still set (SPEC-8 D-150)")
        elif cmd == "secrets-rotate":
            from app import integrations, secretbox
            try:
                n = integrations.rotate(session)
            except (integrations.ConnectionProblem, secretbox.SecretError) as err:
                print(f"secrets-rotate: {err}")
                return 1
            session.commit()
            print(f"secrets-rotate: re-encrypted {n} credential(s) with key "
                  f"{secretbox.keyring()[0][0]}")
        elif cmd == "purge-orphans":
            from app.trips.purge import purge_orphans
            execute = "--execute" in args
            counts = purge_orphans(session, execute=execute)
            if execute:
                session.commit()
            mode = "EXECUTED" if execute else "DRY-RUN — nothing changed; pass --execute"
            print(f"purge-orphans [{mode}]")
            for table, n in counts.items():
                if n:
                    print(f"  {table}: {n}")
            if not any(counts.values()):
                print("  nothing left behind by deleted trips")
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
