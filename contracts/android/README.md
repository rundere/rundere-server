# Android app contract snapshot

The Android app lives in its own repository, **rundere/rundere-android**
(private). `openapi.json` here is a byte-for-byte copy of that app's
`api/openapi.json`: the server's `/api/v1/openapi.json` as of the version the
app was last built against. The app generates its kotlinx.serialization models
from it.

`tests/test_android_contract.py` runs `scripts/schema_drift.py` with this
snapshot against the live `/api/v1` schema, so a server change that would break
the app's decoding, or silently drop one of its writes, fails CI (YONDER-363).
It is the Android counterpart of `contracts/ios/` and `scripts/dto_drift.py`.

Don't edit it here. When the app refreshes its schema, copy it over from the
app repo (byte for byte, keys sorted and 4-space indent as the app saves it).

## What the check reports

It compares operations structurally (path and method, then the JSON request
body and the first 2xx response body, with `$ref`s resolved). Schema names
don't matter: a renamed schema that sends the same data is clean.

| Level | Meaning | Rules |
|---|---|---|
| **ERROR** | The app would break | An operation is gone. A response field the app has as required, non-null and without a default is gone, or now allows null. A field's type changed (integer → number counts; number → integer is fine), including array items and nested objects. A new required request field without a default. A request field the app sends is no longer accepted (it would be dropped silently). A new required query or header parameter, or one that became required. |
| **WARN** | Harmless, but the snapshot is out of date | A nullable or defaulted response field is gone. A query parameter the app sends is gone. The 2xx status code changed. |
| **INFO** | New things the app hasn't adopted | New operations, new optional response or request fields, new enum values. |

Multipart bodies are skipped, and free-form objects and mixed unions are
compatible with anything, because the app reads them as raw JSON (the same
rules as the app's `scripts/gen_models.py`).

Run it on its own:

```bash
uv run python scripts/schema_drift.py                      # schema generated in-process
uv run python scripts/schema_drift.py --openapi spec.json  # or a saved schema
```

## When it fails

- **The change wasn't meant to break the app:** fix the server change (make
  the field optional, keep the old one alongside the new one, add a default).
- **A deliberate breaking change:** the same order as for iOS. Ship app
  support first, then refresh this snapshot from the app repo, then change the
  server. Never edit the snapshot by hand to make CI pass.
