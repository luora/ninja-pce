"""Private first-launch credentials and bounded, process-local admin sessions.

This module grants management authentication only. It neither grants protocol
authority nor implements HTTP trust, origin or CSRF-header policy.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import SCHEMA_VERSION, digest, write_state

_USERNAME = re.compile(r"[a-zA-Z0-9_.-]{1,64}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_HEX = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FILE_BYTES = 8192
_BOOTSTRAP_SECONDS = 30 * 60
_IDLE_SECONDS = 15 * 60
_ABSOLUTE_SECONDS = 8 * 60 * 60
_MAX_SESSIONS = 16
_SCRYPT = {"n": 2**17, "r": 8, "p": 1}


class AuthError(ValueError):
    """A safe, public authentication refusal without credential details."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


@dataclass
class _Session:
    username: str
    csrf_token: str
    created_at: float
    last_used: float


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Nonfinite JSON value")


def _timestamp(value: Any) -> bool:
    try:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _password_bytes(password: Any) -> bytes | None:
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        return None
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        return None
    return encoded if len(encoded) <= 512 else None


def _password_hash(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, **_SCRYPT, maxmem=256 * 1024 * 1024, dklen=32)


class AuthStore:
    """Use only inside an existing process-owned directory with mode0700.

    Bootstrap and credential changes are serialized across threads and store
    instances. Sessions and attempt limits deliberately disappear on restart.
    The HTTP caller owns exact origin/host checking and cookie attributes.
    """

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self.directory = Path(directory).absolute()
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._attempts: deque[float] = deque()
        self._sessions: dict[str, _Session] = {}
        self._check_directory()
        with self._file_lock():
            self._read_auth()
            self._read_bootstrap()

    def _check_directory(self) -> None:
        try:
            info = self.directory.lstat()
            valid = (self.directory.resolve(strict=True) == self.directory and
                     stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and
                     stat.S_IMODE(info.st_mode) == 0o700)
        except (OSError, RuntimeError):
            valid = False
        if not valid:
            raise AuthError(500, "AUTH_DIRECTORY_INVALID", "Management credentials require a private owned directory.")

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self._check_directory()
        descriptor = -1
        try:
            descriptor = os.open(self.directory / ".auth.lock",
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise OSError("Invalid authentication lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            raise AuthError(500, "AUTH_STATE_INVALID", "Management credential state is unavailable or invalid.") from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            descriptor = os.open(self.directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError:
            raise AuthError(500, "AUTH_STATE_INVALID", "Management credential state is unavailable or invalid.") from None
        try:
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > _FILE_BYTES):
                    raise ValueError("Invalid credential file")
                raw = handle.read(_FILE_BYTES + 1)
            if len(raw) > _FILE_BYTES:
                raise ValueError("Credential file exceeds limit")
            document = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_invalid_constant)
            if not isinstance(document, dict):
                raise ValueError("Credential file is not an object")
            checksum = document.pop("checksum", None)
            if (type(document.get("version")) is not int or document["version"] != SCHEMA_VERSION or
                    not isinstance(checksum, str) or not _HEX.fullmatch(checksum) or
                    not hmac.compare_digest(checksum, digest(document))):
                raise ValueError("Invalid credential checksum")
            return document
        except (OSError, ValueError, TypeError, RecursionError):
            raise AuthError(500, "AUTH_STATE_INVALID", "Management credential state is unavailable or invalid.") from None

    def _read_auth(self) -> dict[str, Any] | None:
        data = self._read("auth.json")
        if data is None:
            return None
        if (set(data) != {"version", "kind", "username", "salt", "password_hash", "scrypt", "created_at"} or
                data.get("kind") != "management_admin_auth" or
                not isinstance(data.get("username"), str) or not _USERNAME.fullmatch(data["username"]) or
                not isinstance(data.get("salt"), str) or not _HEX.fullmatch(data["salt"]) or
                not isinstance(data.get("password_hash"), str) or not _HEX.fullmatch(data["password_hash"]) or
                not isinstance(data.get("scrypt"), dict) or data["scrypt"] != _SCRYPT or
                any(type(value) is not int for value in data["scrypt"].values()) or
                not _timestamp(data.get("created_at"))):
            raise AuthError(500, "AUTH_STATE_INVALID", "Management credential state is unavailable or invalid.")
        return data

    def _read_bootstrap(self) -> dict[str, Any] | None:
        data = self._read("bootstrap.json")
        if data is None:
            return None
        if (set(data) != {"version", "kind", "token_sha256", "created_at", "expires_at"} or
                data.get("kind") != "management_admin_bootstrap" or
                not isinstance(data.get("token_sha256"), str) or not _HEX.fullmatch(data["token_sha256"]) or
                not _timestamp(data.get("created_at")) or not _timestamp(data.get("expires_at")) or
                not 0 < data["expires_at"] - data["created_at"] <= _BOOTSTRAP_SECONDS):
            raise AuthError(500, "AUTH_STATE_INVALID", "Management credential state is unavailable or invalid.")
        return data

    def _write(self, name: str, data: dict[str, Any]) -> None:
        self._check_directory()
        try:
            write_state(self.directory / name, data)
        except OSError:
            raise AuthError(500, "AUTH_STATE_UNAVAILABLE", "Management credential state could not be saved.") from None

    def _new_bootstrap(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        self._write("bootstrap.json", {"kind": "management_admin_bootstrap",
                                      "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                                      "created_at": now, "expires_at": now + _BOOTSTRAP_SECONDS})
        return token

    def initialize(self) -> str:
        """Create a one-use bootstrap token without replacing existing state."""
        with self._lock, self._file_lock():
            if self._read_auth() is not None:
                raise AuthError(409, "ADMIN_ALREADY_CLAIMED", "The management administrator is already configured.")
            if self._read_bootstrap() is not None:
                raise AuthError(409, "BOOTSTRAP_ALREADY_INITIALIZED", "Bootstrap is already initialized; rotate it locally.")
            return self._new_bootstrap()

    @property
    def bootstrap_required(self) -> bool:
        """Whether management still lacks an administrator; never expose hashes."""
        with self._lock, self._file_lock():
            auth = self._read_auth()
            self._read_bootstrap()
            return auth is None

    def rotate_bootstrap(self) -> str:
        """Invalidate the previous bootstrap token; never reset an administrator."""
        with self._lock, self._file_lock():
            if self._read_auth() is not None:
                raise AuthError(409, "ADMIN_ALREADY_CLAIMED", "The management administrator is already configured.")
            self._read_bootstrap()
            return self._new_bootstrap()

    def _attempt(self) -> None:
        now = self._monotonic()
        while self._attempts and now - self._attempts[0] >= 60:
            self._attempts.popleft()
        if len(self._attempts) >= 5:
            raise AuthError(429, "AUTH_RATE_LIMITED", "Too many authentication attempts; try again in one minute.")
        self._attempts.append(now)

    def _purge_sessions(self, now: float) -> None:
        self._sessions = {token: session for token, session in self._sessions.items()
                          if 0 <= now - session.last_used < _IDLE_SECONDS and
                          0 <= now - session.created_at < _ABSOLUTE_SECONDS}

    def _session(self, username: str) -> dict[str, str]:
        now = self._monotonic()
        self._purge_sessions(now)
        if len(self._sessions) >= _MAX_SESSIONS:
            raise AuthError(429, "AUTH_SESSION_LIMIT", "Too many active management sessions; sign out or wait for expiry.")
        token = secrets.token_urlsafe(32)
        session = _Session(username, secrets.token_urlsafe(32), now, now)
        self._sessions[token] = session
        return {"token": token, "csrf_token": session.csrf_token, "username": username}

    def claim(self, token: str, username: str, password: str) -> dict[str, str]:
        """Atomically create the sole administrator and consume bootstrap."""
        with self._lock, self._file_lock():
            self._attempt()
            if self._read_auth() is not None:
                raise AuthError(409, "ADMIN_ALREADY_CLAIMED", "The management administrator is already configured.")
            bootstrap = self._read_bootstrap()
            if bootstrap is None:
                raise AuthError(409, "BOOTSTRAP_NOT_INITIALIZED", "Initialize bootstrap locally before claiming management.")
            if self._clock() >= bootstrap["expires_at"]:
                raise AuthError(410, "BOOTSTRAP_EXPIRED", "Bootstrap has expired; rotate it locally.")
            if (not isinstance(token, str) or not _TOKEN.fullmatch(token) or not hmac.compare_digest(
                    hashlib.sha256(token.encode("ascii")).hexdigest(), bootstrap["token_sha256"])):
                raise AuthError(403, "BOOTSTRAP_INVALID", "The bootstrap token is invalid.")
            encoded = _password_bytes(password)
            if not isinstance(username, str) or not _USERNAME.fullmatch(username) or encoded is None:
                raise AuthError(400, "ADMIN_CREDENTIALS_INVALID",
                                "Use an ASCII administrator name and a password of 15 to 128 characters.")
            salt = secrets.token_bytes(32)
            try:
                hashed = _password_hash(encoded, salt)
            except (ValueError, OSError, MemoryError):
                raise AuthError(503, "AUTH_HASH_UNAVAILABLE", "Password authentication is temporarily unavailable.") from None
            self._write("auth.json", {"kind": "management_admin_auth", "username": username,
                                      "salt": salt.hex(), "password_hash": hashed.hex(),
                                      "scrypt": dict(_SCRYPT), "created_at": self._clock()})
            # Committing auth first means a crash can never reopen enrollment.
            # A retained bootstrap file cannot be used once auth.json exists.
            try:
                (self.directory / "bootstrap.json").unlink()
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                # The committed credentials remain authoritative after a failed
                # cleanup; future claims are refused before reading bootstrap.
                pass
            return self._session(username)

    def login(self, username: str, password: str) -> dict[str, str]:
        with self._lock, self._file_lock():
            self._attempt()
            auth = self._read_auth()
            encoded = _password_bytes(password)
            valid = False
            if auth is not None and encoded is not None:
                try:
                    hashed = _password_hash(encoded, bytes.fromhex(auth["salt"]))
                except (ValueError, OSError, MemoryError):
                    raise AuthError(503, "AUTH_HASH_UNAVAILABLE", "Password authentication is temporarily unavailable.") from None
                valid = (isinstance(username, str) and _USERNAME.fullmatch(username) is not None and
                         hmac.compare_digest(username.encode("ascii"), auth["username"].encode("ascii")) and
                         hmac.compare_digest(hashed, bytes.fromhex(auth["password_hash"])))
            if auth is None or not valid:
                raise AuthError(401, "AUTHENTICATION_FAILED", "Administrator name or password is invalid.")
            return self._session(auth["username"])

    def authenticate(self, cookie_token: str) -> dict[str, str] | None:
        with self._lock:
            self._check_directory()
            now = self._monotonic()
            self._purge_sessions(now)
            if not isinstance(cookie_token, str) or not _TOKEN.fullmatch(cookie_token):
                return None
            session = self._sessions.get(cookie_token)
            if session is None:
                return None
            session.last_used = now
            return {"token": cookie_token, "csrf_token": session.csrf_token, "username": session.username}

    def csrf_valid(self, session: dict[str, Any], token: str) -> bool:
        with self._lock:
            if not isinstance(session, dict) or not isinstance(token, str) or not _TOKEN.fullmatch(token):
                return False
            cookie_token = session.get("token")
            if not isinstance(cookie_token, str) or not _TOKEN.fullmatch(cookie_token):
                return False
            self._purge_sessions(self._monotonic())
            current = self._sessions.get(cookie_token)
            return (current is not None and session.get("username") == current.username and
                    hmac.compare_digest(current.csrf_token, token))

    def logout(self, cookie_token: str) -> None:
        with self._lock:
            if isinstance(cookie_token, str):
                self._sessions.pop(cookie_token, None)
