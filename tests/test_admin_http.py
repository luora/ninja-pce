"""Real loopback HTTP checks for the opt-in guided administration boundary."""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ninja_pce.admin import create_admin_server
from ninja_pce.admin_model import default_setup
from ninja_pce.admin_store import AdminStore, initialize_admin

PASSWORD = "local administration test password"
_MISSING = object()


@dataclass
class AdminHTTP:
    root: Path
    bootstrap: str
    store: AdminStore
    server: Any
    cookie: str | None = None
    csrf: str | None = None

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def request(self, path: str, *, method: str = "GET", body: Any = _MISSING,
                headers: list[tuple[str, str]] | None = None, origin: Any = _MISSING,
                cookie: Any = _MISSING, csrf: Any = _MISSING, timeout: float = 3) -> tuple[int, Any, Any]:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not _MISSING else b""
        selected = [("Host", self.authority)]
        if method == "POST":
            selected.extend([("Content-Type", "application/json"), ("Content-Length", str(len(raw)))])
            actual_origin = f"http://{self.authority}" if origin is _MISSING else origin
            if actual_origin is not None:
                selected.append(("Origin", actual_origin))
        actual_cookie = self.cookie if cookie is _MISSING else cookie
        actual_csrf = self.csrf if csrf is _MISSING else csrf
        if actual_cookie is not None:
            selected.append(("Cookie", actual_cookie))
        if method == "POST" and actual_csrf is not None:
            selected.append(("X-CSRF-Token", actual_csrf))
        if headers is not None:
            overridden = {key.lower() for key, _ in headers}
            selected = [(key, value) for key, value in selected if key.lower() not in overridden]
            selected.extend(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in selected:
                connection.putheader(key, value)
            connection.endheaders(raw or None)
            response = connection.getresponse()
            data = response.read()
            value = json.loads(data) if data and response.getheader("Content-Type", "").startswith("application/json") else data
            return response.status, value, response.headers
        finally:
            connection.close()

    def claim(self) -> dict[str, Any]:
        status, value, headers = self.request("/api/admin/bootstrap", method="POST", body={
            "token": self.bootstrap, "username": "operator", "password": PASSWORD})
        assert status == 200, value
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = value["csrf_token"]
        return value

    def preview(self) -> dict[str, Any]:
        status, value, _ = self.request("/api/admin/preview", method="POST", body={
            "expected_revision": None, "setup": default_setup()})
        assert status == 200, value
        return value


@pytest.fixture
def admin_http(tmp_path: Path, monkeypatch):
    # AuthStore has separate tests for the production scrypt parameters. These
    # HTTP tests exercise cookie/session policy while avoiding repeated 128MiB
    # derivations for each independent framing and origin case.
    monkeypatch.setattr("ninja_pce.admin_auth._password_hash",
                        lambda password, salt: hashlib.sha256(salt + password).digest())
    root, bootstrap = initialize_admin(tmp_path / "private-admin")
    store = AdminStore(root, grpcurl="ninja-test-grpcurl-not-installed")
    server = create_admin_server(store, port=0)
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    client = AdminHTTP(root, bootstrap, store, server)
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        store.close()


@pytest.mark.parametrize("path", ["/dashboard", "/app.js", "/styles.css", "/api/admin/config",
                                  "/api/admin/status", "/api/admin/audit", "/api/controller", "/readyz",
                                  "/api/tunnel-options"])
def test_private_routes_require_session_and_ignore_asserted_principal(admin_http, path):
    status, value, _ = admin_http.request(path, headers=[("X-Ninja-Principal", "operator")])
    assert status == 401
    assert value["error"] == "AUTHENTICATION_REQUIRED"
    assert "setup" not in value and "sources" not in value


def test_public_assets_liveness_and_session_do_not_expose_secrets(admin_http, capsys):
    bootstrap_state = json.loads((admin_http.root / "admin/bootstrap.json").read_text())
    secrets = [admin_http.bootstrap, bootstrap_state["token_sha256"]]
    for path in ("/setup", "/setup.js", "/setup.css", "/healthz", "/api/admin/session"):
        status, value, headers = admin_http.request(path)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        encoded = value.decode() if isinstance(value, bytes) else json.dumps(value)
        assert not any(secret in encoded for secret in secrets)
    status, value, _ = admin_http.request("/api/admin/session")
    assert status == 200 and value == {"bootstrap_required": True, "authenticated": False}
    status, _, headers = admin_http.request("/")
    assert status == 302 and headers["Location"] == "/setup"
    status, value, _ = admin_http.request("/healthz", method="HEAD")
    assert status == 200 and value == b""
    assert not any(secret in capsys.readouterr().err for secret in secrets)


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1", "attacker.invalid:8080", "127.0.0.1:1"])
def test_every_request_rejects_alternate_host(admin_http, host):
    status, value, _ = admin_http.request("/setup", headers=[("Host", host)])
    assert status == 403 and value["error"] == "HOST_REJECTED"


@pytest.mark.parametrize("path", ["/api/admin/bootstrap", "/api/admin/login", "/api/admin/logout",
                                  "/api/admin/preview", "/api/admin/apply", "/api/admin/runtime"])
@pytest.mark.parametrize("origin", [None, "http://attacker.invalid", "null", "https://127.0.0.1:8080"])
def test_all_mutations_require_exact_origin_before_authentication(admin_http, path, origin):
    status, value, _ = admin_http.request(path, method="POST", body={}, origin=origin)
    assert status == 403 and value["error"] == "ORIGIN_REJECTED"
    assert not (admin_http.root / "admin/auth.json").exists()


@pytest.mark.parametrize("whitespace", ["", " ", "\t"])
def test_cross_site_fetch_metadata_and_ambiguous_cookie_are_rejected(admin_http, whitespace):
    status, value, _ = admin_http.request("/setup", headers=[("Sec-Fetch-Site", "cross-site")])
    assert status == 403 and value["error"] == "ORIGIN_REJECTED"
    admin_http.claim()
    status, value, _ = admin_http.request("/api/admin/config", cookie=admin_http.cookie + "; ninja_admin" + whitespace + "=second-session")
    assert status == 400 and value["error"] == "INVALID_COOKIE"


def test_claim_session_cookie_logout_and_bootstrap_replay(admin_http):
    session = admin_http.claim()
    assert set(session) == {"authenticated", "username", "csrf_token"}
    status, value, _ = admin_http.request("/api/admin/session")
    assert status == 200 and value == {"bootstrap_required": False, **session}
    assert "token" not in value
    status, value, _ = admin_http.request("/api/admin/bootstrap", method="POST", body={
        "token": admin_http.bootstrap, "username": "other", "password": PASSWORD})
    assert status == 409 and value["error"] == "ADMIN_ALREADY_CLAIMED"
    status, value, headers = admin_http.request("/api/admin/logout", method="POST", body={})
    assert status == 200 and value == {"authenticated": False}
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert "Max-Age=0" in headers["Set-Cookie"]
    status, value, _ = admin_http.request("/api/admin/config")
    assert status == 401 and value["error"] == "AUTHENTICATION_REQUIRED"
    status, value, headers = admin_http.request("/api/admin/login", method="POST", body={
        "username": "operator", "password": PASSWORD}, cookie=None, csrf=None)
    assert status == 200 and value["authenticated"]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert "Path=/" in headers["Set-Cookie"] and "Max-Age=28800" in headers["Set-Cookie"]
    assert not any(secret in json.dumps(value) for secret in [PASSWORD, admin_http.bootstrap,
                                                            headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]])


@pytest.mark.parametrize("csrf", [None, "invalid-token"])
def test_authenticated_mutation_requires_current_csrf(admin_http, csrf):
    admin_http.claim()
    status, value, _ = admin_http.request("/api/admin/preview", method="POST", csrf=csrf,
                                        body={"expected_revision": None, "setup": default_setup()})
    assert status == 403 and value["error"] == "CSRF_REJECTED"
    assert not list((admin_http.root / "revisions").iterdir())


def test_csrf_cannot_be_reused_with_another_session(admin_http):
    admin_http.claim()
    first_csrf = admin_http.csrf
    status, second, headers = admin_http.request("/api/admin/login", method="POST", body={
        "username": "operator", "password": PASSWORD})
    assert status == 200 and second["csrf_token"] != first_csrf
    second_cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, value, _ = admin_http.request("/api/admin/preview", method="POST", cookie=second_cookie,
                                        csrf=first_csrf, body={"expected_revision": None, "setup": default_setup()})
    assert status == 403 and value["error"] == "CSRF_REJECTED"
    status, _, _ = admin_http.request("/api/admin/preview", method="POST", cookie=second_cookie,
                                    csrf=second["csrf_token"], body={"expected_revision": None, "setup": default_setup()})
    assert status == 200


@pytest.mark.parametrize("header", ["Host", "Origin", "Cookie", "Content-Length", "Content-Type", "X-CSRF-Token"])
def test_duplicate_security_and_framing_headers_rejected(admin_http, header):
    values = {"Host": admin_http.authority, "Origin": "http://" + admin_http.authority,
              "Cookie": "ninja_admin=none", "Content-Length": "2", "Content-Type": "application/json",
              "X-CSRF-Token": "none"}
    status, value, _ = admin_http.request("/api/admin/login", method="POST", body=b"{}",
                                        headers=[(header, values[header]), (header, values[header])])
    assert status == 400 and value["error"] == "AMBIGUOUS_REQUEST"


@pytest.mark.parametrize("headers,expected", [([("Transfer-Encoding", "chunked")], 400),
                                             ([("Content-Length", "1048577")], 413),
                                             ([("Content-Length", "-1")], 413),
                                             ([("Content-Length", "+2")], 413),
                                             ([("Content-Type", "text/plain")], 415),
                                             ([("X-Large-Header", "x" * 17000)], 400)])
def test_invalid_request_framing_fails_before_credentials(admin_http, headers, expected):
    status, value, _ = admin_http.request("/api/admin/login", method="POST", body=b"{}", headers=headers)
    assert status == expected, value
    assert not (admin_http.root / "admin/auth.json").exists()


@pytest.mark.parametrize("raw", [b'{"username":"one","username":"two","password":"secret"}',
                                 b'{"username":"one","password":NaN}', b'[]', b'{', b'\xff'])
def test_strict_json_rejects_duplicate_nonfinite_nonobject_and_invalid_input(admin_http, raw):
    status, value, _ = admin_http.request("/api/admin/login", method="POST", body=raw)
    assert status == 400 and value["error"] == "INVALID_INPUT"
    assert not (admin_http.root / "admin/auth.json").exists()


@pytest.mark.parametrize("target", ["/api/admin/session?token=hidden", "/setup#fragment", "http://attacker.invalid/setup"])
def test_alternate_targets_and_query_tokens_rejected(admin_http, target):
    status, value, _ = admin_http.request(target)
    assert status == 400 and value["error"] == "INVALID_TARGET"


def test_valid_wizard_saves_without_network_and_dashboard_remains_observation_only(admin_http, capsys):
    admin_http.claim()
    status, config, _ = admin_http.request("/api/admin/config")
    assert status == 200 and config == {"revision": None, "setup": default_setup(), "source_management": "external"}
    preview = admin_http.preview()
    assert preview["expected_revision"] is None and preview["write_authority"] is False
    assert {"setup.json", "controller.json", "gobgpd.toml"} <= preview["files"].keys()
    assert json.loads(preview["files"]["controller.json"])["operating_mode"] == "observe"
    assert admin_http.store.current_revision() is None
    status, applied, _ = admin_http.request("/api/admin/apply", method="POST", body={
        "expected_revision": None, "preview_id": preview["preview_id"], "activate": False})
    assert status == 200 and applied["status"] == "SAVED"
    assert applied["revision"] == preview["revision"]
    status, value, _ = admin_http.request("/api/admin/status")
    assert status == 200 and value["revision"] == applied["revision"]
    assert value["runtime"]["running"] is False and value["readiness"]["ready"] is False
    status, snapshot, _ = admin_http.request("/api/controller")
    assert status == 200 and snapshot["operating_mode"] == "observe"
    assert snapshot["read_only"] is True and snapshot["readiness"]["ready"] is False
    status, options, _ = admin_http.request("/api/tunnel-options")
    assert status == 200 and options["enabled"] is False and options["policy"]["read_only"] is True
    status, dashboard, _ = admin_http.request("/dashboard")
    assert status == 200 and b'href="/setup"' in dashboard
    status, replay, _ = admin_http.request("/api/admin/apply", method="POST", body={
        "expected_revision": applied["revision"], "preview_id": preview["preview_id"]})
    assert status == 409 and replay["error"] == "PREVIEW_REQUIRED"
    auth_state = json.loads((admin_http.root / "admin/auth.json").read_text())
    secrets = [PASSWORD, admin_http.bootstrap, auth_state["password_hash"], auth_state["salt"]]
    for path in ("/api/admin/config", "/api/admin/status", "/api/admin/audit", "/api/controller"):
        _, value, _ = admin_http.request(path)
        assert not any(secret in json.dumps(value) for secret in secrets)
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err for secret in secrets)


@pytest.mark.parametrize("revision", ["../escape", "x" * 64, "0" * 63, 0, False, [], {}])
def test_malformed_expected_revision_is_rejected_without_writes(admin_http, revision):
    admin_http.claim()
    status, value, _ = admin_http.request("/api/admin/preview", method="POST", body={
        "expected_revision": revision, "setup": default_setup()})
    assert status in {400, 409}, value
    assert not list((admin_http.root / "revisions").iterdir())
    assert not (admin_http.root / "admin/active.json").exists()


def test_management_liveness_and_status_remain_available_during_activation(admin_http, monkeypatch):
    admin_http.claim()
    preview = admin_http.preview()
    entered, release = threading.Event(), threading.Event()
    responses: list[tuple[int, Any, Any]] = []

    def blocked_start(_revision):
        entered.set()
        assert release.wait(timeout=5), "Test failed to release activation"

    monkeypatch.setattr(admin_http.store.runtime, "start", blocked_start)
    worker = threading.Thread(target=lambda: responses.append(admin_http.request(
        "/api/admin/apply", method="POST", timeout=6,
        body={"expected_revision": None, "preview_id": preview["preview_id"], "activate": True})))
    worker.start()
    try:
        assert entered.wait(timeout=3)
        status, value, _ = admin_http.request("/healthz", timeout=1)
        assert status == 200 and value["management"] is True
        status, session, _ = admin_http.request("/api/admin/session", timeout=1)
        assert status == 200 and session["authenticated"]
        status, value, _ = admin_http.request("/api/admin/status", timeout=1)
        assert status == 200 and value["readiness"]["ready"] is False
        assert value["transaction"]["status"] == "APPLYING"
    finally:
        release.set()
        worker.join(timeout=7)
    assert not worker.is_alive()
    assert responses and responses[0][0] == 200 and responses[0][1]["status"] == "ACTIVE"


def test_incomplete_body_times_out_and_management_recovers(admin_http, monkeypatch):
    original = admin_http.server.get_request

    def short_timeout():
        connection, address = original()
        connection.settimeout(0.05)
        return connection, address

    monkeypatch.setattr(admin_http.server, "get_request", short_timeout)
    status, value, _ = admin_http.request("/api/admin/login", method="POST", body=b"{}",
                                        headers=[("Content-Length", "100")])
    assert status == 503 and value["error"] == "LOCAL_STATE_UNAVAILABLE"
    status, value, _ = admin_http.request("/healthz")
    assert status == 200 and value["management"]
