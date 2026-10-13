"""Bounded HTTP over a permissioned Unix socket for qualified lab creation.

Only the configured proxy UID may supply an authenticated principal. This
listener has no TCP endpoint, deletion route, CLI action or CORS support.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import socket
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .cspf import CspfNoPath, CspfSearchLimit
from .pcep_creation import CreationSession

_HEADERS = 8192
_BODY = 1024
_COOKIE = "__Host-ninja-pce-session"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_PRINCIPAL = re.compile(r"[A-Za-z0-9_.@-]{1,128}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_OPERATION = re.compile(r"/api/tunnel-operations/([0-9a-f]{32})\Z")
_PLAN = re.compile(r"[0-9a-f]{64}\Z")
_RSVP_CHECKS = {"identity", "destination", "bandwidth", "ero", "signaling"}
_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict", 413: "Payload Too Large",
            415: "Unsupported Media Type", 431: "Request Header Fields Too Large", 503: "Service Unavailable"}


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


class ProvisioningManager(Protocol):
    headends: dict[str, str]
    allowed_destinations: tuple[str, ...]
    max_bandwidth_bps: int
    max_outstanding: int
    policy_digest: str

    def list(self, principal: str | None = None) -> list[dict[str, Any]]: ...


class ProvisioningBackend(Protocol):
    """An explicitly qualified backend with bounded generated plans.

    No method accepts operator-supplied CLI. Independent verification concerns
    headend RSVP signaling, never traffic forwarding or end-to-end delivery.
    """

    requires_plan: bool
    backend_name: str
    independent_verification: bool

    @property
    def manager(self) -> ProvisioningManager: ...

    def readiness(self, headend: str | None = None) -> tuple[bool, str]: ...

    async def preview(self, principal: str, intent: dict[str, Any]) -> dict[str, Any]: ...

    async def create(self, principal: str, key: str, intent: dict[str, Any], *,
                     approved_plan_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class _Session:
    principal: str
    csrf: str
    expires: float


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class TunnelAPI:
    """Call ``start`` only after preparing a private, process-owned socket directory.

    The directory may grant its prepared group read/search permission, but
    neither group write nor any world permissions. The socket is mode0660 with
    that group. ``qualified`` defaults false independently of the private lab
    console; enabling this API never bypasses the session writer's own guards.
    """

    def __init__(self, creation: CreationSession | ProvisioningBackend, socket_path: Path, allowed_proxy_uid: int,
                 origin: str, allowed_operator: tuple[str, ...], qualified: bool = False):
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                origin != f"https://{parsed.netloc}" or not parsed.netloc.isascii()):
            raise ValueError("Tunnel API requires an exact HTTPS origin without path or credentials")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Invalid tunnel API origin port") from exc
        if type(allowed_proxy_uid) is not int or allowed_proxy_uid < 0:
            raise ValueError("An exact trusted proxy UID is required")
        if (not isinstance(allowed_operator, tuple) or not 1 <= len(allowed_operator) <= 32 or
                len(set(allowed_operator)) != len(allowed_operator) or
                any(not isinstance(value, str) or not _PRINCIPAL.fullmatch(value) for value in allowed_operator)):
            raise ValueError("A bounded exact operator principal allowlist is required")
        if type(qualified) is not bool:
            raise ValueError("Tunnel API qualification must be explicit boolean policy")
        self.creation, self.path = creation, Path(socket_path)
        if not self.path.is_absolute():
            raise ValueError("Tunnel API socket path must be absolute")
        self.allowed_proxy_uid, self.origin, self.host = allowed_proxy_uid, origin, parsed.netloc
        self.allowed_operator, self.qualified = allowed_operator, qualified
        self.listener: asyncio.Server | None = None
        self._sessions: dict[str, _Session] = {}
        self._active = 0
        self._inode: int | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        parent = self.path.parent.lstat()
        if (self.listener is not None or self.path.parent.resolve() != self.path.parent or
                not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or
                stat.S_IMODE(parent.st_mode) & 0o027):
            raise ValueError("Tunnel API needs a prepared process-owned directory without group write/world access")
        if self.path.exists() or self.path.is_symlink():
            raise ValueError("Tunnel API socket already exists; reconcile the previous listener first")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
            self._inode = self.path.lstat().st_ino
            os.chown(self.path, -1, parent.st_gid)
            self.path.chmod(0o660)
            observed = self.path.lstat()
            if (not stat.S_ISSOCK(observed.st_mode) or observed.st_uid != os.getuid() or
                    observed.st_gid != parent.st_gid or stat.S_IMODE(observed.st_mode) != 0o660):
                raise ValueError("Tunnel API socket ownership or mode does not match its prepared directory")
            sock.listen(16)
            sock.setblocking(False)
            self.listener = await asyncio.start_unix_server(self._accept, sock=sock, limit=_HEADERS + 1)
        except BaseException:
            sock.close()
            self._unlink_owned()
            raise

    def _unlink_owned(self) -> None:
        try:
            if self._inode is not None and self.path.lstat().st_ino == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._inode = None

    async def close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._unlink_owned()
        self._sessions.clear()

    def _uses_plan(self) -> bool:
        return getattr(self.creation, "requires_plan", False) is True

    def _ready(self, headend: str | None = None) -> tuple[bool, str]:
        if not self.qualified:
            return False, ("Router provisioning has not been qualified on this headend." if self._uses_plan()
                           else "RSVP creation has not been qualified on this headend.")
        if self._uses_plan():
            try:
                return cast(ProvisioningBackend, self.creation).readiness(headend)
            except (ValueError, OSError, RuntimeError):
                return False, "Provisioning observation or controller policy is unavailable."
        creation = cast(CreationSession, self.creation)
        manager = creation.manager
        try:
            manager.computer.validated_snapshot()
            names = [headend] if headend is not None else list(manager.headends)
            for name in names:
                try:
                    creation._connection(manager.headends[name])
                    return True, "Qualified lab creation is available; forwarding remains unverified."
                except (ValueError, KeyError):
                    continue
        except (ValueError, OSError, RuntimeError):
            return False, "Fresh, consistent topology is unavailable."
        return False, "A fresh synchronized enabled RSVP-initiation session is unavailable."

    def _checked_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        if operation.get("status") == "VERIFIED" or operation.get("verified") is True:
            proof = operation.get("verification")
            if (not self._uses_plan() or
                    getattr(self.creation, "independent_verification", False) is not True or
                    not isinstance(proof, dict) or proof.get("independent") is not True or
                    proof.get("scope") != "headend-rsvp" or
                    proof.get("pcep_control") is not True or
                    not isinstance(proof.get("checks"), list) or
                    len(proof["checks"]) != len(_RSVP_CHECKS) or
                    any(not isinstance(check, str) for check in proof["checks"]) or
                    set(proof["checks"]) != _RSVP_CHECKS):
                raise _HTTPError(503, "Creation acknowledgment cannot establish independent RSVP verification.")
        return operation

    def _cookie_session(self, headers: dict[str, str], principal: str) -> tuple[str, _Session] | None:
        tokens = []
        for part in headers.get("cookie", "").split(";"):
            name, separator, value = part.strip().partition("=")
            if name == _COOKIE and separator:
                tokens.append(value)
        if len(tokens) != 1 or not _TOKEN.fullmatch(tokens[0]):
            return None
        session = self._sessions.get(tokens[0])
        if session is None or session.principal != principal or time.monotonic() >= session.expires:
            return None
        return tokens[0], session

    def _session(self, headers: dict[str, str], principal: str) -> tuple[str, _Session]:
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires > now}
        existing = self._cookie_session(headers, principal)
        if existing is not None:
            return existing
        if len(self._sessions) >= 128:
            raise _HTTPError(503, "Too many operator sessions; retry after expiry.")
        token = secrets.token_urlsafe(32)
        session = _Session(principal, secrets.token_urlsafe(32), now + 900)
        self._sessions[token] = session
        return token, session

    @staticmethod
    async def _request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        except asyncio.LimitOverrunError as exc:
            raise _HTTPError(431, "Request headers exceed the bounded limit.") from exc
        except asyncio.IncompleteReadError as exc:
            raise _HTTPError(400, "Incomplete HTTP request.") from exc
        if len(raw) > _HEADERS:
            raise _HTTPError(431, "Request headers exceed the bounded limit.")
        try:
            lines = raw.decode("ascii").split("\r\n")
        except UnicodeError as exc:
            raise _HTTPError(400, "HTTP headers must use ASCII.") from exc
        request = lines[0].split(" ")
        if len(request) != 3 or request[2] != "HTTP/1.1" or not request[1].startswith("/"):
            raise _HTTPError(400, "Only origin-form HTTP/1.1 requests are accepted.")
        headers: dict[str, str] = {}
        for line in lines[1:-2]:
            key, separator, value = line.partition(":")
            if (not separator or not _HEADER_NAME.fullmatch(key) or key.lower() in headers or
                    any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise _HTTPError(400, "Malformed or duplicate HTTP header.")
            headers[key.lower()] = value.strip()
        if "transfer-encoding" in headers or "expect" in headers or "trailer" in headers:
            raise _HTTPError(400, "Streaming, trailers and continuation requests are unsupported.")
        length = headers.get("content-length", "0")
        if not re.fullmatch(r"0|[1-9][0-9]{0,8}", length):
            raise _HTTPError(400, "A valid bounded Content-Length is required.")
        amount = int(length)
        if amount > _BODY:
            raise _HTTPError(413, "Request body exceeds 1024 bytes.")
        if request[0] != "POST" and amount:
            raise _HTTPError(400, "Read requests must not contain a body.")
        try:
            body = await asyncio.wait_for(reader.readexactly(amount), 3)
        except asyncio.IncompleteReadError as exc:
            raise _HTTPError(400, "Incomplete HTTP body.") from exc
        return request[0], request[1], headers, body

    async def _route(self, method: str, path: str, headers: dict[str, str], body: bytes
                     ) -> tuple[int, dict[str, Any], str | None]:
        principal = headers.get("x-ninja-principal", "")
        if principal not in self.allowed_operator:
            raise _HTTPError(403, "This authenticated principal is not an authorized operator.")
        if headers.get("host") != self.host or ("origin" in headers and headers["origin"] != self.origin):
            raise _HTTPError(403, "Request host or origin does not match this controller.")
        if method == "GET" and path == "/api/tunnel-options":
            enabled, reason = self._ready()
            token, session = self._session(headers, principal)
            manager = self.creation.manager
            uses_plan = self._uses_plan()
            options = {"headends": list(manager.headends), "destinations": list(manager.allowed_destinations),
                       "max_bandwidth_bps": manager.max_bandwidth_bps, "enabled": enabled, "reason": reason,
                       "backend": getattr(self.creation, "backend_name", "pcep-initiation"),
                       "requires_plan": uses_plan,
                       "policy": {"revision": manager.policy_digest,
                                  "max_outstanding": getattr(manager, "max_outstanding", 1),
                                  "setup_priority": 7, "hold_priority": 7, "read_only": not enabled,
                                  "forwarding_verification": "unverified",
                                  "operational_verification": "headend-rsvp" if uses_plan else "unverified"},
                       "csrf_token": session.csrf}
            cookie = f"{_COOKIE}={token}; Path=/; Max-Age=900; Secure; HttpOnly; SameSite=Strict"
            return 200, options, cookie
        match = _OPERATION.fullmatch(path)
        if method == "GET" and match:
            operations = self.creation.manager.list(principal)
            operation = next((item for item in operations if item["id"] == match.group(1)), None)
            if operation is None:
                raise _HTTPError(404, "Unknown tunnel operation.")
            return 200, {"operation": self._checked_operation(operation)}, None
        is_preview = path == "/api/tunnel-plans" and self._uses_plan()
        if path != "/api/tunnels" and not is_preview:
            raise _HTTPError(404, "Unknown API route.")
        if method != "POST":
            raise _HTTPError(405, "Tunnel creation requires POST.")
        if headers.get("origin") != self.origin:
            raise _HTTPError(403, "An exact same-origin request is required.")
        authenticated_session = self._cookie_session(headers, principal)
        csrf = headers.get("x-csrf-token", "")
        if (authenticated_session is None or not _TOKEN.fullmatch(csrf) or
                not secrets.compare_digest(authenticated_session[1].csrf, csrf)):
            raise _HTTPError(403, "A current principal-bound session and CSRF token are required.")
        if headers.get("content-type", "").lower() != "application/json":
            raise _HTTPError(415, "Creation requires application/json.")
        key = headers.get("idempotency-key", "")
        if not is_preview and not _KEY.fullmatch(key):
            raise _HTTPError(400, "A bounded Idempotency-Key is required.")
        try:
            intent = json.loads(body.decode("utf-8"), object_pairs_hook=_object,
                                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise _HTTPError(400, "Malformed or duplicate-field JSON intent.") from exc
        manager = self.creation.manager
        fields = {"headend", "destination", "bandwidth_bps"}
        uses_plan = self._uses_plan()
        if uses_plan and not is_preview:
            fields.add("approved_plan_id")
        if (not isinstance(intent, dict) or set(intent) != fields or
                not isinstance(intent["headend"], str) or intent["headend"] not in manager.headends or
                not isinstance(intent["destination"], str) or intent["destination"] not in manager.allowed_destinations or
                intent["headend"] == intent["destination"] or type(intent["bandwidth_bps"]) is not int or
                not 0 < intent["bandwidth_bps"] <= manager.max_bandwidth_bps):
            raise _HTTPError(400, "Intent must use authorized headend, destination and positive integer bandwidth.")
        plan_id = intent.get("approved_plan_id")
        if uses_plan and not is_preview and (not isinstance(plan_id, str) or not _PLAN.fullmatch(plan_id)):
            raise _HTTPError(400, "Creation requires the exact generated plan identifier.")
        intent = {name: intent[name] for name in ("headend", "destination", "bandwidth_bps")}
        if uses_plan and not is_preview:
            # Recovery is an authenticated read, not new delivery authority.
            # Do this before readiness so a lost response can be recovered
            # while the PCC/TED is unavailable or qualification is withdrawn.
            previous = [op for op in manager.list(principal) if op.get("idempotency_key") == key]
            if previous:
                if (len(previous) != 1 or previous[0].get("intent") != intent or
                        previous[0].get("plan_id") != plan_id):
                    raise _HTTPError(409, "Idempotency key is already bound to another exact intent and plan.")
                return 202, {"operation": self._checked_operation(previous[0])}, None
        enabled, reason = self._ready(intent["headend"])
        if not enabled:
            raise _HTTPError(503, reason)
        try:
            if uses_plan:
                backend = cast(ProvisioningBackend, self.creation)
                if is_preview:
                    plan = await asyncio.wait_for(backend.preview(principal, intent), 10)
                    return 200, {"plan": plan}, None
                operation = await asyncio.wait_for(backend.create(principal, key, intent,
                                                                  approved_plan_id=cast(str, plan_id)), 10)
            else:
                operation = await asyncio.wait_for(cast(CreationSession, self.creation).create(principal, key, intent), 10)
        except CspfNoPath as exc:
            raise _HTTPError(409, "No admissible directed path has sufficient bandwidth.") from exc
        except CspfSearchLimit as exc:
            raise _HTTPError(503, "Path admission could not establish a bounded result.") from exc
        except ValueError as exc:
            raise _HTTPError(409, str(exc)[:256]) from exc
        return 202, {"operation": self._checked_operation(operation)}, None

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, document: dict[str, Any],
                       cookie: str | None = None) -> None:
        body = json.dumps(document, allow_nan=False, separators=(",", ":")).encode()
        headers = [f"HTTP/1.1 {status} {_REASONS[status]}", "Content-Type: application/json; charset=utf-8",
                   f"Content-Length: {len(body)}", "Cache-Control: no-store", "Connection: close",
                   "X-Content-Type-Options: nosniff", "Content-Security-Policy: default-src 'none'",
                   "Referrer-Policy: no-referrer"]
        if cookie is not None:
            headers.append(f"Set-Cookie: {cookie}")
        if status == 405:
            headers.append("Allow: POST")
        try:
            writer.write("\r\n".join(headers).encode("ascii") + b"\r\n\r\n" + body)
            await asyncio.wait_for(writer.drain(), 1)
        except (OSError, asyncio.TimeoutError):
            pass

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        active = False
        task = asyncio.current_task()
        try:
            sock = writer.get_extra_info("socket")
            if sock is None or sock.family != socket.AF_UNIX:
                raise _HTTPError(403, "A trusted Unix proxy connection is required.")
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.allowed_proxy_uid:
                raise _HTTPError(403, "The Unix peer is not the configured trusted proxy.")
            if self._active >= 8:
                raise _HTTPError(503, "Tunnel API connection limit reached.")
            self._active += 1
            active = True
            if task is not None:
                self._tasks.add(task)
            request = await self._request(reader)
            status, document, cookie = await self._route(*request)
            await self._respond(writer, status, document, cookie)
        except _HTTPError as exc:
            await self._respond(writer, exc.status, {"error": exc.message})
        except asyncio.TimeoutError:
            await self._respond(writer, 408, {"error": "Bounded request deadline expired; inspect the operation before retrying."})
        except (ValueError, OSError, RuntimeError):
            await self._respond(writer, 503, {"error": "Controller observation or operation state is unavailable."})
        finally:
            if active:
                self._active -= 1
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
