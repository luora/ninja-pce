"""Bootstrap, credential persistence and session security boundaries."""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ninja_pce import admin_auth
from ninja_pce.admin_auth import AuthError, AuthStore
from ninja_pce.state import load_state, write_state

PASSWORD = "long private admin password"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def private(tmp_path):
    directory = tmp_path / "admin"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(private, clock):
    return AuthStore(private, clock=clock, monotonic=clock)


@pytest.fixture
def fast_hash(monkeypatch):
    """Session/IO edge cases do not each need a 128MiB password derivation."""
    monkeypatch.setattr(admin_auth, "_password_hash", lambda password, salt: hashlib.sha256(salt + password).digest())


def claimed(store):
    token = store.initialize()
    return store.claim(token, "operator", PASSWORD)


def refusal(code, action, *args):
    with pytest.raises(AuthError) as caught:
        action(*args)
    assert caught.value.code == code
    assert str(caught.value) == caught.value.message
    return caught.value


def test_bootstrap_is_hashed_expiring_private_and_rotates(store, private, clock):
    assert store.bootstrap_required is True
    token = store.initialize()
    assert len(token) == 43
    raw = (private / "bootstrap.json").read_text()
    assert token not in raw
    data = load_state(private / "bootstrap.json")
    assert data["token_sha256"] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert data["expires_at"] == clock.now + 1800
    assert (private / "bootstrap.json").stat().st_mode & 0o777 == 0o600
    assert refusal("BOOTSTRAP_ALREADY_INITIALIZED", store.initialize).status == 409
    replacement = store.rotate_bootstrap()
    assert replacement != token
    assert refusal("BOOTSTRAP_INVALID", store.claim, token, "operator", PASSWORD).status == 403


def test_real_scrypt_claim_login_and_no_plaintext_credentials(store, private, monkeypatch):
    original = hashlib.scrypt
    calls = []

    def record(password, **options):
        calls.append(options.copy())
        return original(password, **options)

    monkeypatch.setattr(hashlib, "scrypt", record)
    enrollment = store.initialize()
    session = store.claim(enrollment, "operator", PASSWORD)
    assert set(session) == {"token", "csrf_token", "username"}
    assert session["username"] == "operator"
    assert len(session["token"]) == len(session["csrf_token"]) == 43
    assert session["token"] != session["csrf_token"]
    assert not (private / "bootstrap.json").exists()
    raw = (private / "auth.json").read_text()
    assert PASSWORD not in raw and enrollment not in raw
    assert session["token"] not in raw and session["csrf_token"] not in raw
    data = load_state(private / "auth.json")
    assert len(bytes.fromhex(data["salt"])) == len(bytes.fromhex(data["password_hash"])) == 32
    assert (private / "auth.json").stat().st_mode & 0o777 == 0o600
    second = store.login("operator", PASSWORD)
    assert second["token"] != session["token"]
    assert len(calls) == 2
    for options in calls:
        assert options["n"] == 2**17 and options["r"] == 8 and options["p"] == 1
        assert options["maxmem"] == 256 * 1024 * 1024 and options["dklen"] == 32


def test_expired_bootstrap_must_be_rotated(store, clock, fast_hash):
    token = store.initialize()
    clock.advance(1800)
    assert refusal("BOOTSTRAP_EXPIRED", store.claim, token, "operator", PASSWORD).status == 410
    renewed = store.rotate_bootstrap()
    assert store.claim(renewed, "operator", PASSWORD)["username"] == "operator"


def test_claim_requires_local_initialization(store):
    assert refusal("BOOTSTRAP_NOT_INITIALIZED", store.claim, "x" * 43, "operator", PASSWORD).status == 409


@pytest.mark.parametrize("username", ["", "name with space", "ümlaut", "a" * 65, "\ud800", None])
def test_claim_rejects_invalid_names_without_claiming(store, private, username, fast_hash):
    token = store.initialize()
    assert refusal("ADMIN_CREDENTIALS_INVALID", store.claim, token, username, PASSWORD).status == 400
    assert not (private / "auth.json").exists()
    assert (private / "bootstrap.json").exists()


@pytest.mark.parametrize("password", ["short", "x" * 14, "x" * 129, "\ud800" * 15, None])
def test_claim_rejects_invalid_passwords(store, private, password, fast_hash):
    token = store.initialize()
    refusal("ADMIN_CREDENTIALS_INVALID", store.claim, token, "operator", password)
    assert not (private / "auth.json").exists()


def test_unicode_password_maximum_is_supported(store, fast_hash):
    password = "\U0001f511" * 128
    session = store.claim(store.initialize(), "operator", password)
    assert store.authenticate(session["token"]) is not None
    assert store.login("operator", password)["username"] == "operator"


def test_existing_admin_refuses_claim_initialization_and_rotation(store, private, fast_hash):
    session = claimed(store)
    assert store.bootstrap_required is False
    before = (private / "auth.json").read_bytes()
    for action, args in [(store.initialize, ()), (store.rotate_bootstrap, ()),
                         (store.claim, ("x" * 43, "attacker", PASSWORD))]:
        assert refusal("ADMIN_ALREADY_CLAIMED", action, *args).status == 409
    assert (private / "auth.json").read_bytes() == before
    assert store.authenticate(session["token"])["username"] == "operator"


def test_cross_instance_claim_race_has_one_winner(private, clock, fast_hash):
    first = AuthStore(private, clock=clock, monotonic=clock)
    token = first.initialize()
    second = AuthStore(private, clock=clock, monotonic=clock)

    def attempt(store, name):
        try:
            return store.claim(token, name, PASSWORD)
        except AuthError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt, first, "first"), executor.submit(attempt, second, "second")]
        results = [future.result() for future in futures]
    successes = [value for value in results if isinstance(value, dict)]
    assert len(successes) == 1 and results.count("ADMIN_ALREADY_CLAIMED") == 1
    assert load_state(private / "auth.json")["username"] == successes[0]["username"]


def test_committed_auth_blocks_claim_after_interrupted_bootstrap_cleanup(store, private, monkeypatch, fast_hash):
    token = store.initialize()
    original = Path.unlink

    def fail_bootstrap(path, *args, **kwargs):
        if path.name == "bootstrap.json":
            raise OSError("sensitive local path must never be exposed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_bootstrap)
    session = store.claim(token, "operator", PASSWORD)
    assert (private / "bootstrap.json").exists()
    restarted = AuthStore(private)
    refusal("ADMIN_ALREADY_CLAIMED", restarted.claim, token, "attacker", PASSWORD)
    assert restarted.login("operator", PASSWORD)["username"] == session["username"]


def test_login_failure_is_generic_and_shares_bootstrap_rate_limit(store, clock, fast_hash):
    token = store.initialize()
    for _ in range(2):
        refusal("BOOTSTRAP_INVALID", store.claim, "x" * 43, "operator", PASSWORD)
    claimed_session = store.claim(token, "operator", PASSWORD)
    messages = []
    for username, password in [("other", PASSWORD), ("operator", "incorrect but long password")]:
        error = refusal("AUTHENTICATION_FAILED", store.login, username, password)
        assert error.status == 401
        messages.append(str(error))
    assert messages[0] == messages[1]
    assert refusal("AUTH_RATE_LIMITED", store.login, "operator", PASSWORD).status == 429
    assert store.authenticate(claimed_session["token"]) is not None
    clock.advance(60)
    assert store.login("operator", PASSWORD)["username"] == "operator"


@pytest.mark.parametrize("username", ["\ud800", "ümlaut", None, "a" * 10000])
def test_login_malformed_username_has_safe_refusal(store, username, fast_hash):
    claimed(store)
    refusal("AUTHENTICATION_FAILED", store.login, username, PASSWORD)


def test_session_logout_and_csrf_binding(store, fast_hash):
    first = claimed(store)
    second = store.login("operator", PASSWORD)
    assert store.csrf_valid(first, first["csrf_token"])
    assert not store.csrf_valid(first, second["csrf_token"])
    assert not store.csrf_valid({**first, "username": "other"}, first["csrf_token"])
    assert not store.csrf_valid(first, "malformed")
    returned = store.authenticate(first["token"])
    returned["username"] = "attacker"
    assert store.authenticate(first["token"])["username"] == "operator"
    store.logout(first["token"])
    assert store.authenticate(first["token"]) is None
    assert not store.csrf_valid(first, first["csrf_token"])
    assert store.authenticate(second["token"]) is not None


def test_idle_expiry_refresh_and_absolute_expiry(store, clock, fast_hash):
    session = claimed(store)
    clock.advance(899)
    assert store.authenticate(session["token"]) is not None
    clock.advance(899)
    assert store.authenticate(session["token"]) is not None
    clock.advance(900)
    assert store.authenticate(session["token"]) is None
    clock.advance(60)
    active = store.login("operator", PASSWORD)
    for _ in range(32):
        clock.advance(899)
        assert store.authenticate(active["token"]) is not None
    clock.advance(32)
    assert store.authenticate(active["token"]) is None


def test_sessions_do_not_survive_restart(store, private, clock, fast_hash):
    session = claimed(store)
    restarted = AuthStore(private, clock=clock, monotonic=clock)
    assert restarted.authenticate(session["token"]) is None
    assert not restarted.csrf_valid(session, session["csrf_token"])
    assert restarted.login("operator", PASSWORD)["username"] == "operator"


def test_session_cap_is_enforced_and_logout_releases_slot(store, clock, fast_hash):
    sessions = [claimed(store)]
    for _ in range(15):
        clock.advance(61)
        sessions.append(store.login("operator", PASSWORD))
        for session in sessions:
            store.authenticate(session["token"])
    assert refusal("AUTH_SESSION_LIMIT", store.login, "operator", PASSWORD).status == 429
    store.logout(sessions[0]["token"])
    assert store.login("operator", PASSWORD)["username"] == "operator"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777])
def test_insecure_directory_is_refused(private, mode):
    private.chmod(mode)
    refusal("AUTH_DIRECTORY_INVALID", AuthStore, private)


def test_symlinked_directory_is_refused(private, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)
    refusal("AUTH_DIRECTORY_INVALID", AuthStore, alias)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "directory", "hardlink", "public", "oversized"])
def test_unsafe_credential_files_are_refused(store, private, tmp_path, replacement, fast_hash):
    claimed(store)
    path = private / "auth.json"
    original = path.read_bytes()
    path.unlink()
    target = tmp_path / "target"
    target.write_bytes(original)
    target.chmod(0o600)
    if replacement == "symlink":
        path.symlink_to(target)
    elif replacement == "fifo":
        os.mkfifo(path, 0o600)
    elif replacement == "directory":
        path.mkdir(mode=0o600)
    elif replacement == "hardlink":
        os.link(target, path)
    elif replacement == "public":
        path.write_bytes(original)
        path.chmod(0o644)
    else:
        path.write_bytes(b"x" * 8193)
        path.chmod(0o600)
    error = refusal("AUTH_STATE_INVALID", AuthStore, private)
    assert str(target) not in str(error) and PASSWORD not in str(error)


@pytest.mark.parametrize("contents", [b"{}", b"[]", b"{\"version\":2,\"version\":2}",
                                      b"{\"created_at\":NaN}", b"\xff", b"{" * 2000])
def test_invalid_json_credential_files_fail_closed(private, contents):
    path = private / "auth.json"
    path.write_bytes(contents)
    path.chmod(0o600)
    refusal("AUTH_STATE_INVALID", AuthStore, private)


def test_scrypt_parameter_tampering_is_refused(store, private, fast_hash):
    claimed(store)
    data = load_state(private / "auth.json")
    data["scrypt"]["n"] = 2**20
    write_state(private / "auth.json", data)
    refusal("AUTH_STATE_INVALID", AuthStore, private)


def test_credential_checksum_tampering_is_refused(store, private, fast_hash):
    claimed(store)
    path = private / "auth.json"
    data = json.loads(path.read_text())
    data["username"] = "attacker"
    path.write_text(json.dumps(data))
    refusal("AUTH_STATE_INVALID", AuthStore, private)


def test_changed_directory_permissions_invalidate_sessions(store, private, fast_hash):
    session = claimed(store)
    private.chmod(0o755)
    refusal("AUTH_DIRECTORY_INVALID", store.authenticate, session["token"])


def test_password_derivation_failure_does_not_consume_bootstrap(store, private, monkeypatch):
    token = store.initialize()

    def unavailable(*args):
        raise MemoryError("sensitive internals")

    monkeypatch.setattr(admin_auth, "_password_hash", unavailable)
    error = refusal("AUTH_HASH_UNAVAILABLE", store.claim, token, "operator", PASSWORD)
    assert error.status == 503 and "sensitive" not in str(error)
    assert not (private / "auth.json").exists() and (private / "bootstrap.json").exists()
