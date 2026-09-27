"""YONDER-58 — the /api/v1 contract (OpenAPI behind auth) and the iOS DTO
drift check (SPEC-4.2 §3.1). The drift test runs the real
ios/Yonder/Models/DTOs.swift against the live schema, so a server change that
would break the app's decoding fails CI.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from fastapi.testclient import TestClient
    DEPS = True
except ImportError:
    DEPS = False

if DEPS:
    import dto_drift
    from app import authn
    from app.config import settings
    from app.db import SessionLocal, engine
    from app.main import app
    from app.models import Base, User

    Base.metadata.create_all(engine)
    settings.auth_cookie_secure = False
    client = TestClient(app)

    def _h():
        with SessionLocal() as s:
            u = s.query(User).filter_by(username="contract-user").one_or_none()
            if u is None:
                u = User(username="contract-user", auth_source="local")
                s.add(u)
                s.commit()
            return {"Authorization": f"Bearer {authn.issue_access_token(u)}"}

    # ------------------------------------------------------------------ the schema endpoint
    def test_openapi_behind_auth_and_api_only():
        assert client.get("/api/v1/openapi.json").status_code == 401
        r = client.get("/api/v1/openapi.json", headers=_h())
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["title"] == "Run.Dere API"
        assert spec["paths"] and all(p.startswith("/api/v1") for p in spec["paths"])
        assert "/api/v1/trips/{trip_id}" in spec["paths"]
        assert "/api/v1/openapi.json" not in spec["paths"]
        names = set(spec["components"]["schemas"])
        assert {"TripSnapshot", "UserOut", "TokenOut", "ReviewItemDetail"} <= names
        user = spec["components"]["schemas"]["UserOut"]["properties"]
        assert {"email", "share_with_admins", "sharing_prompt", "opt_out_blocker"} <= set(user)

    def test_no_public_schema_or_docs():
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code in (404, 200)      # "/docs" isn't an API route
        assert "swagger" not in client.get("/docs").text.lower()
        assert client.get("/api/v1/docs").status_code == 401
        r = client.get("/api/v1/docs", headers=_h())
        assert r.status_code == 200 and "/api/v1/openapi.json" in r.text

    # ------------------------------------------------------------------ the drift check
    def test_ios_dtos_match_the_contract():
        spec = client.get("/api/v1/openapi.json", headers=_h()).json()
        structs = dto_drift.parse_swift(dto_drift.DEFAULT_SWIFT.read_text())
        assert {"TripSnapshot", "UserPublic", "CaptureIn"} <= set(structs)
        errors = [m for lvl, m in dto_drift.check(structs, spec) if lvl == "ERROR"]
        assert errors == []

    def test_drift_rules():
        spec = {"components": {"schemas": {
            "Thing": {"properties": {
                "id": {"type": "string"},
                "maybe": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "count": {"type": "integer"},
                "blob": {},
                "brand_new": {"type": "string"}}},
            "ThingIn": {"properties": {"name": {"type": "string"}}}}}}
        swift = """
        struct Thing: Codable {
            let id: String
            let maybe: String          // nullable on the server → must be optional
            let count: String          // wrong type
            let blob: JSONValue        // JSONValue decodes null itself
            let gone: String?          // optional, no longer sent
            let vanished: Int          // required, no longer sent
        }
        struct ThingIn: Codable {
            var name: String?
            var extra: String?         // server would drop it
        }
        struct Missing: Codable { let a: String }
        """
        found = dto_drift.check(dto_drift.parse_swift(swift), spec)
        text = "\n".join(f"{lvl} {m}" for lvl, m in found)
        assert "ERROR Thing.maybe: the server may send null" in text
        assert "ERROR Thing.count: Swift String vs JSON ['integer']" in text
        assert "Thing.blob" not in text
        assert "WARN Thing.gone: not in Thing any more" in text
        assert "ERROR Thing.vanished: required by the app" in text
        assert "ERROR ThingIn.extra: the server doesn't accept" in text
        assert "ERROR Missing: no schema" in text
        assert "INFO Thing: not adopted yet: brand_new" in text

    def test_drift_cli_exit_codes(tmp_path, capsys):
        good = tmp_path / "ok.swift"
        good.write_text("struct CaptureOut: Codable { let batch_id: String }")
        assert dto_drift.main(["--swift", str(good), "--quiet"]) == 0
        bad = tmp_path / "bad.swift"
        bad.write_text("struct CaptureOut: Codable { let nope: String }")
        assert dto_drift.main(["--swift", str(bad)]) == 1
        assert "1 error(s)" in capsys.readouterr().out
