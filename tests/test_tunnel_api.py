"""Unix HTTP trust/CSRF/schema boundaries; all creation calls are offline fakes."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import stat
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from ninja_pce.pcep_creation import CreationSession
from ninja_pce.tunnel_api import TunnelAPI

ORIGIN = "https://controller.example.test"
INTENT = {"headend": "PE1", "destination": "PE3", "bandwidth_bps": 1_000_000}


class Manager:
    headends = {"PE1": "10.255.0.1"}
    allowed_destinations = ("PE3",)
    max_bandwidth_bps = 1_000_000
    policy_digest = "a" * 64

    def __init__(self):
        self.computer = self
        self.topology_ready = True
        self.operations: dict[tuple[str, str], dict[str, Any]] = {}

    def validated_snapshot(self):
        if not self.topology_ready:
            raise ValueError("collector unavailable")
        return None, "b" * 64

    def list(self, principal=None):
        return copy.deepcopy([value for (owner, _), value in self.operations.items()
                              if principal is None or principal == owner])


class Creation:
    def __init__(self):
        self.manager = Manager()
        self.connection_ready = True
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _connection(self, peer):
        if not self.connection_ready:
            raise ValueError("session unavailable or kill switch set")
        return object()

    async def create(self, principal, key, intent):
        self.calls.append((principal, key, copy.deepcopy(intent)))
        scope = (principal, key)
        previous = self.manager.operations.get(scope)
        if previous is not None:
            if previous["intent"] != intent:
                raise ValueError("Idempotency key conflicts with a different creation intent")
            return copy.deepcopy(previous)
        operation = {"id": f"{len(self.manager.operations) + 1:032x}", "principal": principal,
                     "status": "APPLIED_UNVERIFIED", "verified": False, "intent": copy.deepcopy(intent)}
        self.manager.operations[scope] = operation
        return copy.deepcopy(operation)


@asynccontextmanager
async def running(tmp_path: Path, *, qualified: bool = True, **options):
    parent = tmp_path / "api"
    parent.mkdir()
    parent.chmod(0o750)
    creation = Creation()
    api = TunnelAPI(cast(CreationSession, creation), parent / "http.sock", options.pop("allowed_proxy_uid", os.getuid()),
                    ORIGIN, ("operator", "second"), qualified=qualified, **options)
    await api.start()
    try:
        yield api, creation
    finally:
        await api.close()


async def exchange(api: TunnelAPI, method="GET", path="/api/tunnel-options", *,
                   headers: dict[str, str] | None = None, body: bytes = b"", extra_headers: str = ""):
    request_headers = {"Host": "controller.example.test", "X-Ninja-Principal": "operator"}
    request_headers.update(headers or {})
    if body and "Content-Length" not in request_headers:
        request_headers["Content-Length"] = str(len(body))
    raw = (f"{method} {path} HTTP/1.1\r\n" +
           "\r\n".join(f"{key}: {value}" for key, value in request_headers.items()) +
           "\r\n" + extra_headers + "\r\n").encode() + body
    reader, writer = await asyncio.open_unix_connection(api.path)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        lines = head[:-4].decode().split("\r\n")
        response_headers = dict(line.split(": ", 1) for line in lines[1:])
        payload = await asyncio.wait_for(reader.readexactly(int(response_headers["Content-Length"])), 5)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    assert int(response_headers["Content-Length"]) == len(payload)
    return int(lines[0].split(" ")[1]), response_headers, json.loads(payload)


async def authorization(api: TunnelAPI):
    status, headers, body = await exchange(api)
    assert status == 200
    return {"Cookie": headers["Set-Cookie"].split(";", 1)[0], "X-CSRF-Token": body["csrf_token"],
            "Origin": ORIGIN, "Content-Type": "application/json", "Idempotency-Key": "request-one"}


def test_options_session_and_creation_use_only_permissioned_unix_socket(tmp_path: Path) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            assert api.listener is not None
            assert all(sock.family == socket.AF_UNIX for sock in api.listener.sockets)
            observed = api.path.stat()
            assert stat.S_IMODE(observed.st_mode) == 0o660 and observed.st_gid == api.path.parent.stat().st_gid
            status, headers, options = await exchange(api)
            assert status == 200 and options["enabled"] is True
            assert options["headends"] == ["PE1"] and options["destinations"] == ["PE3"]
            assert options["max_bandwidth_bps"] == 1_000_000
            assert all(flag in headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"))
            assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
            assert not any("access-control" in key.lower() for key in headers)
            auth = await authorization(api)
            status, _, result = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(INTENT).encode())
            assert status == 202 and result["operation"]["status"] == "APPLIED_UNVERIFIED"
            assert result["operation"]["verified"] is False
            assert creation.calls == [("operator", "request-one", INTENT)]
            op_id = result["operation"]["id"]
            status, _, fetched = await exchange(api, path=f"/api/tunnel-operations/{op_id}")
            assert status == 200 and fetched == result
        assert not api.path.exists()
    asyncio.run(scenario())


def test_untrusted_unix_uid_cannot_supply_an_authenticated_identity(tmp_path: Path) -> None:
    async def scenario():
        async with running(tmp_path, allowed_proxy_uid=os.getuid() + 1) as (api, creation):
            status, _, _ = await exchange(api)
            assert status == 403 and creation.calls == [] and not api._sessions
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["unqualified", "topology", "session_or_kill"])
def test_unqualified_or_unhealthy_backend_cannot_schedule_creation(tmp_path: Path, unavailable: str) -> None:
    async def scenario():
        async with running(tmp_path, qualified=unavailable != "unqualified") as (api, creation):
            if unavailable == "topology":
                creation.manager.topology_ready = False
            elif unavailable == "session_or_kill":
                creation.connection_ready = False
            status, _, options = await exchange(api)
            assert status == 200 and options["enabled"] is False
            if unavailable == "unqualified":
                assert options["reason"] == "RSVP creation has not been qualified on this headend."
            auth = await authorization(api)
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(INTENT).encode())
            assert status == 503 and creation.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["host", "origin", "missing_origin", "role", "csrf", "missing_cookie",
                                     "principal_cookie", "duplicate_cookie", "expired_session"])
def test_mutation_requires_exact_role_origin_host_and_principal_bound_csrf(tmp_path: Path, changed: str) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            auth = await authorization(api)
            if changed == "host":
                auth["Host"] = "attacker.example.test"
            elif changed == "origin":
                auth["Origin"] = "https://attacker.example.test"
            elif changed == "missing_origin":
                del auth["Origin"]
            elif changed == "role":
                auth["X-Ninja-Principal"] = "viewer"
            elif changed == "csrf":
                auth["X-CSRF-Token"] = "z" * 43
            elif changed == "missing_cookie":
                del auth["Cookie"]
            elif changed == "principal_cookie":
                auth["X-Ninja-Principal"] = "second"
            elif changed == "duplicate_cookie":
                auth["Cookie"] += "; " + auth["Cookie"]
            else:
                api._sessions = {key: replace(value, expires=0) for key, value in api._sessions.items()}
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(INTENT).encode())
            assert status == 403 and creation.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    b'{"headend":"PE1","headend":"PE1","destination":"PE3","bandwidth_bps":1000000}',
    b'{"headend":"PE1","destination":"PE3","bandwidth_bps":NaN}', b"[]", b"{", b"\xff",
    json.dumps({**INTENT, "bandwidth_bps": True}).encode(),
    json.dumps({**INTENT, "bandwidth_bps": 1.0}).encode(),
    json.dumps({**INTENT, "bandwidth_bps": "1000000"}).encode(),
    json.dumps({**INTENT, "bandwidth_bps": 1_000_001}).encode(),
    json.dumps({**INTENT, "headend": "P1"}).encode(),
    json.dumps({**INTENT, "destination": "P4"}).encode(),
    json.dumps({**INTENT, "cli": "configure terminal"}).encode(),
])
def test_strict_bounded_json_intent_rejects_before_any_creation_call(tmp_path: Path, body: bytes) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=await authorization(api), body=body)
            assert status == 400 and creation.calls == []
    asyncio.run(scenario())


@pytest.mark.parametrize(("changed", "expected"), [("media", 415), ("key", 400), ("body_limit", 413),
                                                   ("header_limit", 431), ("transfer", 400), ("duplicate", 400)])
def test_media_lengths_headers_and_idempotency_are_bounded(tmp_path: Path, changed: str, expected: int) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            auth = await authorization(api)
            extra = ""
            if changed == "media":
                auth["Content-Type"] = "application/x-www-form-urlencoded"
            elif changed == "key":
                auth["Idempotency-Key"] = ""
            elif changed == "body_limit":
                auth["Content-Length"] = "1025"
            elif changed == "header_limit":
                auth["X-Padding"] = "x" * 9000
            elif changed == "transfer":
                auth["Transfer-Encoding"] = "chunked"
            else:
                extra = "Host: controller.example.test\r\n"
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                          body=json.dumps(INTENT).encode(), extra_headers=extra)
            assert status == expected and creation.calls == []
    asyncio.run(scenario())


def test_cookie_reuse_does_not_invalidate_inflight_csrf_and_scoped_replay_is_stable(tmp_path: Path) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            auth = await authorization(api)
            status, headers, options = await exchange(api, headers={"Cookie": auth["Cookie"]})
            assert status == 200 and options["csrf_token"] == auth["X-CSRF-Token"]
            assert headers["Set-Cookie"].split(";", 1)[0] == auth["Cookie"]
            first = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(INTENT).encode())
            second = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(INTENT).encode())
            assert first == second and len(creation.manager.operations) == 1
            changed = {**INTENT, "bandwidth_bps": 500_000}
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(changed).encode())
            assert status == 409 and len(creation.manager.operations) == 1
    asyncio.run(scenario())


def test_operation_polling_cannot_read_another_principal_or_claim_verified(tmp_path: Path) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            _, _, response = await exchange(api, "POST", "/api/tunnels", headers=await authorization(api),
                                            body=json.dumps(INTENT).encode())
            path = "/api/tunnel-operations/" + response["operation"]["id"]
            status, _, _ = await exchange(api, path=path, headers={"X-Ninja-Principal": "second"})
            assert status == 404
            creation.manager.operations[("operator", "request-one")]["status"] = "VERIFIED"
            status, _, _ = await exchange(api, path=path)
            assert status == 503
    asyncio.run(scenario())


@pytest.mark.parametrize(("method", "path", "status"), [("GET", "/api/tunnels", 405),
                                                        ("DELETE", "/api/tunnels/owned", 404),
                                                        ("POST", "/api/cli", 404),
                                                        ("OPTIONS", "/api/tunnels", 405),
                                                        ("GET", "/api/tunnel-options?create=true", 404)])
def test_read_routes_never_mutate_and_no_delete_cli_or_cors_routes_exist(
    tmp_path: Path, method: str, path: str, status: int,
) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            actual, _, _ = await exchange(api, method, path)
            assert actual == status and not creation.calls and not creation.manager.operations
    asyncio.run(scenario())


def test_session_table_and_active_connections_are_bounded(tmp_path: Path) -> None:
    async def scenario():
        async with running(tmp_path) as (api, creation):
            for _ in range(128):
                status, _, _ = await exchange(api)
                assert status == 200
            status, _, _ = await exchange(api)
            assert status == 503 and len(api._sessions) == 128
            clients = [await asyncio.open_unix_connection(api.path) for _ in range(8)]
            try:
                async def accepted():
                    while api._active != 8:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(accepted(), 1)
                status, _, _ = await exchange(api)
                assert status == 503 and not creation.calls
            finally:
                for _, writer in clients:
                    writer.close()
                    await writer.wait_closed()
    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["group_write", "world", "symlink", "existing_file"])
def test_socket_directory_and_existing_path_fail_closed_without_overwrite(tmp_path: Path, unsafe: str) -> None:
    async def scenario():
        parent = tmp_path / "parent"
        parent.mkdir(mode=0o750)
        path = parent / "http.sock"
        if unsafe == "group_write":
            parent.chmod(0o770)
        elif unsafe == "world":
            parent.chmod(0o755)
        elif unsafe == "symlink":
            link = tmp_path / "linked"
            link.symlink_to(parent, target_is_directory=True)
            path = link / "http.sock"
        else:
            path.write_text("keep this file")
        api = TunnelAPI(cast(CreationSession, Creation()), path, os.getuid(), ORIGIN, ("operator",))
        with pytest.raises(ValueError):
            await api.start()
        if unsafe == "existing_file":
            assert path.read_text() == "keep this file"
    asyncio.run(scenario())


@pytest.mark.parametrize("origin", ["http://controller.example.test", "https://controller.example.test/",
                                    "https://user:password@controller.example.test", "https://controller.example.test:bad"])
def test_origin_configuration_requires_exact_https_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError):
        TunnelAPI(cast(CreationSession, Creation()), tmp_path / "api.sock", os.getuid(), origin, ("operator",))
