"""YonderClient — a tiny stdlib HTTP client for the Yonder JSON API (Phase 4).

Deliberately dependency-free (urllib), so it is unit-testable and can be verified
against a live stack without the MCP runtime. Every call is authenticated with a
per-user Yonder token, so the client sees exactly what that user may see (the
API is scoped by app/scope.py). Supply a personal API token (Account → Personal
API tokens, `ypat_…`) or log in with a username+password; with a password, a
401 (expiry) makes the client log in again once and retry.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class YonderError(Exception):
    pass


def _q(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def _some(fields: dict) -> dict:
    """Drop unset (None) fields so a PATCH only changes what was given."""
    return {k: v for k, v in fields.items() if v is not None}


class YonderClient:
    def __init__(self, base_url: str, *, username: str | None = None,
                 password: str | None = None, token: str | None = None,
                 timeout: int = 20):
        self.base = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self._token = token
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _login(self) -> None:
        if not (self.username and self.password):
            raise YonderError("no credentials — set YONDER_USER/YONDER_PASSWORD "
                              "or supply YONDER_TOKEN")
        data = self._raw("POST", "/api/v1/auth/login", auth=False,
                         body={"username": self.username, "password": self.password})
        self._token = (data or {}).get("access")
        if not self._token:
            raise YonderError("login did not return an access token")

    def _raw(self, method: str, path: str, *, body=None, auth: bool = True,
             _retry: bool = True):
        headers = {"Accept": "application/json", "User-Agent": "Yonder-MCP"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            if not self._token:
                self._login()
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self.base + path, data=payload,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth and _retry:
                self._login()                       # token expired → re-login once
                return self._raw(method, path, body=body, auth=auth, _retry=False)
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
            raise YonderError(f"{method} {path} → HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            raise YonderError(f"cannot reach Yonder at {self.base}{path}: {e}")

    # ------------------------------------------------------------------ API (one per MCP tool)
    def list_trips(self):
        return self._raw("GET", "/api/v1/trips")

    def get_trip(self, trip_id: str):
        return self._raw("GET", f"/api/v1/trips/{urllib.parse.quote(trip_id)}")

    def dashboard(self):
        return self._raw("GET", "/api/v1/dashboard")

    def review(self, bucket: str = "all"):
        return self._raw("GET", "/api/v1/review?bucket=" + urllib.parse.quote(bucket))

    def capture_url(self, url: str, travel_date: str | None = None,
                    title: str | None = None, notes: str | None = None,
                    travel_time: str | None = None, mode: str | None = None):
        return self._raw("POST", "/api/v1/capture",
                         body={"url": url, "travel_date": travel_date,
                               "title": title, "notes": notes,
                               "travel_time": travel_time, "mode": mode})

    def review_item(self, item_id: str):
        return self._raw("GET", f"/api/v1/review/{_q(item_id)}")

    def assign_trip(self, item_id: str, trip_id: str):
        return self._raw("POST", f"/api/v1/review/{_q(item_id)}/assign-trip",
                         body={"trip_id": trip_id})

    def update_trip(self, trip_id: str, **fields):
        return self._raw("PATCH", f"/api/v1/trips/{_q(trip_id)}", body=_some(fields))

    def activity_types(self):
        return self._raw("GET", "/api/v1/activity-types")

    def add_activity(self, trip_id: str, type: str, values: dict,
                     starts_at: str | None = None, traveler_ids: list | None = None):
        return self._raw("POST", f"/api/v1/trips/{_q(trip_id)}/activities",
                         body={"type": type, "values": values, "starts_at": starts_at,
                               "traveler_ids": traveler_ids})

    def update_activity(self, trip_id: str, activity_id: str, **fields):
        return self._raw("PATCH", f"/api/v1/trips/{_q(trip_id)}/activities/{_q(activity_id)}",
                         body=_some(fields))

    def delete_activity(self, trip_id: str, activity_id: str):
        return self._raw("DELETE", f"/api/v1/trips/{_q(trip_id)}/activities/{_q(activity_id)}")

    def add_packing_item(self, trip_id: str, label: str, category: str = "activity",
                         quantity: int = 1):
        return self._raw("POST", f"/api/v1/trips/{_q(trip_id)}/packing-items",
                         body={"label": label, "category": category, "quantity": quantity})

    def save_research(self, trip_id: str, title: str, body_md: str,
                      place_key: str | None = None, sources: list | None = None,
                      topic: str = "research"):
        return self._raw("POST", f"/api/v1/trips/{_q(trip_id)}/findings",
                         body={"topic": topic, "title": title, "body_md": body_md,
                               "place_key": place_key, "sources": sources or []})

    def journal_create(self, **fields):
        return self._raw("POST", "/api/v1/journal", body=_some(fields))

    def journal_list(self, **filters):
        qs = urllib.parse.urlencode(_some(filters))
        return self._raw("GET", "/api/v1/journal" + (f"?{qs}" if qs else ""))

    def journal_get(self, entry_id: str):
        return self._raw("GET", f"/api/v1/journal/{_q(entry_id)}")

    def journal_update(self, entry_id: str, **fields):
        return self._raw("PATCH", f"/api/v1/journal/{_q(entry_id)}", body=_some(fields))

    def journal_attach(self, entry_id: str, trip_id: str | None):
        return self._raw("PATCH", f"/api/v1/journal/{_q(entry_id)}",
                         body={"trip_id": trip_id, "attach_trip": True})

    def generate_packing(self, trip_id: str):
        return self._raw("POST",
                         f"/api/v1/trips/{urllib.parse.quote(trip_id)}/packing/generate")

    def set_packing_status(self, trip_id: str, item_id: str, status: str):
        return self._raw("PATCH",
                         f"/api/v1/trips/{urllib.parse.quote(trip_id)}"
                         f"/packing-items/{urllib.parse.quote(item_id)}",
                         body={"status": status})
