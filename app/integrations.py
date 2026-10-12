"""Per-user AirTrail / AdventureLog connections (SPEC-8 D-150–D-152,
YONDER-192).

The one place integration clients are built. Every write and lookup that talks
to AirTrail or AdventureLog on behalf of a capture uses the **capture owner's**
connection — their own base URL and token, limited to what that remote user may
do. There are no admin tokens: replacing the interim admin-only gate (D-156),
a user with no connection simply gets Run·dere-only writes (D-148).

- **Secrets (D-151).** The token is encrypted with app/secretbox.py, bound to
  its row, decrypted only to build a client for an outgoing call, and never
  returned, logged or put in an error. With no secrets key, connections can't be
  saved or used.
- **URL safety (D-152).** Base URLs pass app/netpolicy.py when saved, and every
  connection goes through its guarded opener (re-checked at connect time, no
  redirects, size cap).
- **Who owns an ownerless capture.** An open capture (no owner) and auth-off
  mode belong to the deployment owner: the user linked to the `self` traveller,
  else the first admin. Only a deployment with no users at all still uses the
  deprecated TX_* variables directly (the single-user world, unchanged).
- **Migration.** On boot, the deployment owner's connections are seeded once from
  the deprecated TX_AIRTRAIL_* / TX_ADVENTURELOG_* variables, with a warning;
  then those variables can be removed.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import netpolicy, secretbox
from app.models import TravelerIdentity, User, UserIntegration

log = logging.getLogger("yonder.integrations")

AIRTRAIL, ADVENTURELOG = "airtrail", "adventurelog"
KINDS = (AIRTRAIL, ADVENTURELOG)
LABELS = {AIRTRAIL: "AirTrail", ADVENTURELOG: "AdventureLog"}
TIMEOUT_SECONDS = 20


class ConnectionProblem(ValueError):
    """A connection can't be saved or used. The message is safe to show."""


# ------------------------------------------------------------------ reads

def get(session: Session, user_id: str | None, kind: str) -> UserIntegration | None:
    if not user_id or kind not in KINDS:
        return None
    return (session.query(UserIntegration)
            .filter(UserIntegration.user_id == user_id, UserIntegration.kind == kind)
            .one_or_none())


def for_user(session: Session, user_id: str | None) -> dict[str, UserIntegration]:
    if not user_id:
        return {}
    return {c.kind: c for c in session.query(UserIntegration)
            .filter(UserIntegration.user_id == user_id).all()}


def usable(c: UserIntegration | None) -> bool:
    return bool(c and c.enabled and c.credential_ct and secretbox.available())


def owner_user_id(session: Session) -> str | None:
    """The deployment owner: the active user linked to the `self` traveller,
    else the earliest active admin. None when there are no users."""
    me = (session.query(TravelerIdentity.id).filter(TravelerIdentity.kind == "self")
          .order_by(TravelerIdentity.id).first())
    if me is not None:
        u = (session.query(User).filter(User.traveler_id == me[0], User.is_active.is_(True))
             .order_by(User.created_at).first())
        if u is not None:
            return u.id
    u = (session.query(User).filter(User.is_admin.is_(True), User.is_active.is_(True))
         .order_by(User.created_at).first())
    return u.id if u else None


def _has_users(session: Session) -> bool:
    return session.query(User.id).first() is not None


# ------------------------------------------------------------------ clients

def _credential(c: UserIntegration) -> str:
    return secretbox.decrypt(c.credential_ct or "",
                             secretbox.aad_for(c.id, c.user_id, c.kind))


def client(c: UserIntegration | None):
    """An AirTrail / AdventureLog client for this connection, or None when it's
    off, has no usable credential, or the secrets key is missing. Built on the
    guarded opener (D-152)."""
    if not usable(c):
        return None
    try:
        token = _credential(c)
    except secretbox.SecretError as err:
        log.warning("connection %s (%s) unusable: %s", c.id, c.kind, err)
        return None
    kw = {"timeout": TIMEOUT_SECONDS, "opener": netpolicy.guarded_opener(),
          "max_bytes": netpolicy.MAX_RESPONSE_BYTES}
    if c.kind == AIRTRAIL:
        from adapters.targets.airtrail import AirTrailClient
        return AirTrailClient(c.base_url, token, **kw)
    from adapters.targets.adventurelog import AdventureLogClient
    return AdventureLogClient(c.base_url, token, **kw)


def _legacy_clients():
    """The deprecated server-wide clients — only for a deployment with no users
    at all (auth off, never set up accounts)."""
    from app.config import settings
    al = at = None
    if settings.adventurelog_api_key:
        from adapters.targets.adventurelog import AdventureLogClient
        al = AdventureLogClient(settings.adventurelog_url, settings.adventurelog_api_key)
    if settings.airtrail_token:
        from adapters.targets.airtrail import AirTrailClient
        at = AirTrailClient(settings.airtrail_url, settings.airtrail_token)
    return al, at


@dataclass
class Clients:
    """One person's integration clients: what their captures may write to."""
    user_id: str | None
    adventurelog: object = None
    airtrail: object = None
    airtrail_user_id: str | None = None     # whose seat an AirTrail flight is
    public_url: str | None = None           # AdventureLog links for this person

    @property
    def external(self) -> frozenset[str]:
        """Which systems this person's captures may target (D-148)."""
        return frozenset(k for k, v in ((ADVENTURELOG, self.adventurelog),
                                        (AIRTRAIL, self.airtrail)) if v is not None)


def clients_for_user(session: Session, user_id: str | None) -> Clients:
    """The clients for one user (None = the deployment owner). An inactive
    user's connections aren't used."""
    uid = user_id or owner_user_id(session)
    if uid is None:
        if _has_users(session):
            return Clients(None)
        al, at = _legacy_clients()
        from app.config import settings
        return Clients(None, adventurelog=al, airtrail=at,
                       airtrail_user_id=_self_airtrail_id(session),
                       public_url=settings.adventurelog_public_url or None)
    user = session.get(User, uid)
    if user is None or not user.is_active:
        return Clients(uid)
    conns = for_user(session, uid)
    at_conn, al_conn = conns.get(AIRTRAIL), conns.get(ADVENTURELOG)
    return Clients(
        uid, adventurelog=client(al_conn), airtrail=client(at_conn),
        airtrail_user_id=((at_conn.remote_user_id if at_conn else None)
                          or _traveller_airtrail_id(session, user)),
        public_url=(al_conn.public_url if al_conn and al_conn.public_url
                    else (al_conn.base_url if al_conn else None)))


class ClientCache:
    """clients_for_user, once per owner per batch run. `drop` leaves systems out
    for everyone (the CLI's --no-airtrail / --no-adventurelog)."""
    def __init__(self, session: Session, drop: frozenset[str] = frozenset()):
        self._session = session
        self._drop = frozenset(drop)
        self._by_owner: dict[str | None, Clients] = {}

    def __call__(self, owner_id: str | None) -> Clients:
        if owner_id not in self._by_owner:
            c = clients_for_user(self._session, owner_id)
            if ADVENTURELOG in self._drop:
                c.adventurelog = None
            if AIRTRAIL in self._drop:
                c.airtrail = None
            self._by_owner[owner_id] = c
        return self._by_owner[owner_id]


def _traveller_airtrail_id(session: Session, user: User) -> str | None:
    if user.traveler_id is None:
        return None
    t = session.get(TravelerIdentity, user.traveler_id)
    return t.airtrail_user_id if t else None


def _self_airtrail_id(session: Session) -> str | None:
    t = (session.query(TravelerIdentity).filter(TravelerIdentity.kind == "self")
         .order_by(TravelerIdentity.id).first())
    return t.airtrail_user_id if t else None


# ------------------------------------------------------------------ writes

def save(session: Session, user: User, kind: str, *, base_url: str,
         credential: str = "", public_url: str = "", remote_user_id: str = "",
         enabled: bool = True, resolve: bool = True) -> UserIntegration:
    """Create or update the user's connection of this kind. The token is
    required for a new connection and whenever the base URL changes, so a saved
    token is never sent to a host it wasn't entered for. Caller commits."""
    if kind not in KINDS:
        raise ConnectionProblem("unknown integration")
    if not secretbox.available():
        raise ConnectionProblem("connections are off on this server: an admin needs "
                                "to set a secrets key (TX_SECRETS_KEY)")
    try:
        url = netpolicy.check_url(base_url, resolve=resolve)
    except netpolicy.BlockedURL as err:
        raise ConnectionProblem(str(err)) from None
    pub = (public_url or "").strip().rstrip("/")
    if pub and not pub.startswith(("https://", "http://")):
        raise ConnectionProblem("the browser address must start with https://")
    c = get(session, user.id, kind)
    token = (credential or "").strip()
    if c is None:
        if not token:
            raise ConnectionProblem("enter the API token")
        c = UserIntegration(id=str(uuid.uuid4()), user_id=user.id, kind=kind, base_url=url)
        session.add(c)
    elif url != c.base_url and not token:
        raise ConnectionProblem("re-enter the token when you change the URL")
    c.base_url = url
    c.public_url = pub or None
    c.remote_user_id = (remote_user_id or "").strip()[:128] or None
    c.enabled = bool(enabled)
    if token:
        c.credential_ct = secretbox.encrypt(token, secretbox.aad_for(c.id, c.user_id, kind))
        c.credential_hint = secretbox.hint(token)
        c.last_check_at = c.last_check_ok = c.last_error = None
    session.flush()
    return c


def delete(session: Session, c: UserIntegration) -> None:
    """Remove the connection and its ciphertext. Caller commits."""
    session.delete(c)


def purge_user(session: Session, user_id: str) -> int:
    """Every connection of a user (account deletion). Caller commits."""
    return (session.query(UserIntegration).filter(UserIntegration.user_id == user_id)
            .delete(synchronize_session=False))


def check(session: Session, c: UserIntegration) -> tuple[bool, str]:
    """Test the connection with the user's own token; records the outcome.
    The message is short and safe — a status and a reason, never a body."""
    ok, msg = _probe(c)
    c.last_check_at = datetime.now(timezone.utc)
    c.last_check_ok = ok
    c.last_error = None if ok else msg[:300]
    session.flush()
    return ok, msg


def _probe(c: UserIntegration) -> tuple[bool, str]:
    if not secretbox.available():
        return False, "no secrets key is configured on the server"
    cl = client(c) if c.enabled else None
    if cl is None:
        return False, ("this connection is turned off" if not c.enabled
                       else "the saved token can't be read; enter it again")
    path = "/api/flight/list" if c.kind == AIRTRAIL else "/api/collections/"
    try:
        status, parsed, _ = cl._request("GET", path)
    except Exception as err:                         # adapter errors carry no token
        return False, f"can't reach the server ({_reason(err)})"
    if status in (401, 403):
        return False, f"HTTP {status}: the token was refused"
    if status >= 400:
        return False, f"HTTP {status} from the server"
    if c.kind == AIRTRAIL:
        if not isinstance(parsed, dict) or "flights" not in parsed:
            return False, "that doesn't look like AirTrail's API"
        return True, f"connected · {len(parsed.get('flights') or [])} flights"
    if not isinstance(parsed, (dict, list)):
        return False, "that doesn't look like AdventureLog's API"
    n = parsed.get("count") if isinstance(parsed, dict) else len(parsed)
    return True, "connected" + (f" · {n} collections" if isinstance(n, int) else "")


def _reason(err: Exception) -> str:
    """The cause of a connection failure in a few words, without URLs."""
    cause = err.__cause__ or err
    reason = getattr(cause, "reason", None) or cause
    text = str(reason).strip() or type(reason).__name__
    return text[:160]


# ------------------------------------------------------------------ migration

def deprecated_env() -> list[str]:
    """The deprecated TX_* integration variables still set (admin notice)."""
    from app.config import settings
    names = []
    if settings.airtrail_token:
        names.append("TX_AIRTRAIL_TOKEN")
    if settings.adventurelog_api_key:
        names.append("TX_ADVENTURELOG_API_KEY")
    if settings.airtrail_db_url:
        names.append("TX_AIRTRAIL_DB_URL")
    return names


def seed_from_env(session: Session) -> list[str]:
    """Copy the deprecated server-wide credentials into the deployment owner's
    connections, once (D-150). Returns the kinds it created. Never overwrites
    a connection the owner already has."""
    from app.config import settings
    if not deprecated_env():
        return []
    uid = owner_user_id(session)
    if uid is None or not secretbox.available():
        log.warning("TX_AIRTRAIL_* / TX_ADVENTURELOG_* are deprecated (SPEC-8 D-150) "
                    "and not migrated yet: %s", "no secrets key (TX_SECRETS_KEY)"
                    if uid is not None else "no owner account to move them to")
        return []
    user = session.get(User, uid)
    created = []
    plan = ((AIRTRAIL, settings.airtrail_url, settings.airtrail_token, "",
             _traveller_airtrail_id(session, user) or _self_airtrail_id(session)),
            (ADVENTURELOG, settings.adventurelog_url, settings.adventurelog_api_key,
             settings.adventurelog_public_url, ""))
    for kind, url, token, pub, remote in plan:
        if not token or get(session, uid, kind) is not None:
            continue
        try:
            # The operator configured this host, so it's trusted (netpolicy);
            # don't resolve it at boot — the container may start first.
            save(session, user, kind, base_url=url, credential=token, public_url=pub,
                 remote_user_id=remote or "", resolve=False)
            created.append(kind)
        except ConnectionProblem as err:
            log.warning("couldn't migrate the server-wide %s settings: %s", LABELS[kind], err)
    if created:
        session.commit()
        log.warning("moved the server-wide %s settings to %s's connections; the TX_* "
                    "variables are deprecated and can be removed (SPEC-8 D-150) — first "
                    "add their LAN hosts to TX_INTEGRATION_ALLOWED_HOSTS, which keeps "
                    "trusting them once the old variables are gone",
                    " and ".join(LABELS[k] for k in created), user.username)
    return created


# ------------------------------------------------------------------ admin view

def admin_overview(session: Session) -> list[dict]:
    """Who has which connection and its status — never a URL path or credential."""
    from urllib.parse import urlsplit
    users = {u.id: u.username for u in session.query(User).all()}
    rows = []
    for c in (session.query(UserIntegration)
              .order_by(UserIntegration.user_id, UserIntegration.kind).all()):
        rows.append({"user": users.get(c.user_id, "(deleted user)"), "kind": LABELS[c.kind],
                     "host": urlsplit(c.base_url).hostname or "", "enabled": c.enabled,
                     "ok": c.last_check_ok, "checked": c.last_check_at})
    return rows


def rotate(session: Session) -> int:
    """Re-encrypt every credential with the newest key. Returns how many
    changed. Caller commits."""
    ring = secretbox.keyring()
    if not ring:
        raise ConnectionProblem("no secrets key is configured")
    newest = ring[0][0]
    n = 0
    for c in session.query(UserIntegration).filter(UserIntegration.credential_ct.isnot(None)):
        if secretbox.key_id(c.credential_ct) == newest:
            continue
        aad = secretbox.aad_for(c.id, c.user_id, c.kind)
        c.credential_ct = secretbox.encrypt(secretbox.decrypt(c.credential_ct, aad), aad)
        n += 1
    session.flush()
    return n
