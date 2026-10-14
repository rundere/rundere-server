"""Pushover — the first notification target (YONDER-253, D-197).

Needs a Pushover *application* API token (create one at pushover.net/apps,
e.g. "Run·dere") and the *user* (or group) key to deliver to. Optional: one
device, and a priority for alerts (-1 quiet, 0 normal, 1 high); recoveries
always go quiet. The endpoint is fixed — no URL is taken from anyone, so
there's nothing to steer (D-152).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

KIND = "pushover"
LABEL = "Pushover"
API = "https://api.pushover.net/1/messages.json"
TIMEOUT = 10
_KEY = re.compile(r"^[A-Za-z0-9]{30}$")
PRIORITIES = {-1: "Quiet (no sound)", 0: "Normal", 1: "High (bypasses quiet hours)"}


def validate(token: str, user: str, device: str, priority) -> tuple[dict, dict]:
    """(secrets, config) from the form, or ValueError with what's wrong."""
    token, user, device = (token or "").strip(), (user or "").strip(), (device or "").strip()
    if not _KEY.match(token):
        raise ValueError("the application token is 30 letters and digits (pushover.net/apps)")
    if not _KEY.match(user):
        raise ValueError("the user key is 30 letters and digits (on your Pushover dashboard)")
    if device and not re.match(r"^[A-Za-z0-9_-]{1,25}$", device):
        raise ValueError("a device name is up to 25 letters, digits, - or _")
    try:
        p = int(priority)
    except (TypeError, ValueError):
        p = 0
    if p not in PRIORITIES:
        raise ValueError("pick a priority")
    return {"token": token, "user": user}, {"device": device or None, "priority": p}


def hint(secrets: dict) -> str:
    return "…" + secrets.get("user", "")[-4:]


def send(secrets: dict, config: dict, title: str, message: str, *, quiet: bool = False,
         url: str | None = None) -> tuple[bool, str]:
    fields = {"token": secrets["token"], "user": secrets["user"],
              "title": title[:250], "message": message[:1024],
              "priority": -1 if quiet else int(config.get("priority") or 0)}
    if config.get("device"):
        fields["device"] = config["device"]
    if url:
        fields["url"], fields["url_title"] = url, "Run·dere notifications"
    from app.config import settings
    # Our own User-Agent: CDN-fronted APIs refuse Python's default one (Frankfurter's
    # Cloudflare answers it with 403 / error 1010).
    req = urllib.request.Request(API, data=urllib.parse.urlencode(fields).encode(),
                                 method="POST",
                                 headers={"User-Agent": f"Rundere/{settings.app_version}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            return False, f"Pushover answered HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"can't reach Pushover ({type(e).__name__})"
    except ValueError:
        return False, "Pushover's answer couldn't be read"
    if body.get("status") == 1:
        return True, "sent"
    errors = [str(x) for x in body.get("errors") or []] or ["refused"]
    # Pushover's errors name the field ("application token is invalid"), never echo it.
    return False, "Pushover: " + "; ".join(errors)[:200]
