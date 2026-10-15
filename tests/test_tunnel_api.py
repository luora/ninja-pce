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
from ninja_pce.tunnel_api import ProvisioningBackend, TunnelAPI

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


class Provisioning(Creation):
    requires_plan = True
    backend_name = "router-config"
    independent_verification = True

    def __init__(self):
        super().__init__()
        self.manager.max_outstanding = 2
        self.previews: list[tuple[str, dict[str, Any]]] = []
        self.approved_plans: list[str] = []
        self.admissions: list[tuple[str, str]] = []

    def readiness(self, headend=None):
        return (self.connection_ready and self.manager.topology_ready, "Configuration provisioning available.")

    async def preview(self, principal, intent):
        self.previews.append((principal, copy.deepcopy(intent)))
        return {"plan_id": "e" * 64, "expires_at": "2099-01-01T00:00:00+00:00",
                "rendered_config": "interface Tunnel200\n tunnel mpls traffic-eng bandwidth 1000\n",
                "path_nodes": ["PE1", "P1", "P2", "PE3"], **intent}

    async def create(self, principal, key, intent, *, approved_plan_id):
        self.admissions.append((principal, key))
        if approved_plan_id != "e" * 64:
            raise ValueError("Generated plan does not match the approved identifier")
        ready, reason = self.readiness(intent["headend"])
        if not ready:
            operation = {"id": f"{len(self.manager.operations) + 1:032x}", "principal": principal,
                         "status": "REJECTED", "verified": False, "intent": copy.deepcopy(intent),
                         "idempotency_key": key, "plan_id": approved_plan_id, "reason": reason,
                         "authorization": None, "tunnel_id": None, "reservation_id": None}
            self.manager.operations[(principal, key)] = operation
            return copy.deepcopy(operation)
        self.approved_plans.append(approved_plan_id)
        operation = await super().create(principal, key, intent)
        operation.update(status="VERIFIED", verified=True,
                         idempotency_key=key, plan_id=approved_plan_id,
                         verification={"independent": True, "scope": "headend-rsvp", "pcep_control": True,
                                       "checks": ["identity", "destination", "bandwidth", "ero", "signaling"]})
        self.manager.operations[(principal, key)] = copy.deepcopy(operation)
        return operation


@asynccontextmanager
async def running(tmp_path: Path, *, qualified: bool = True, **options):
    parent = tmp_path / "api"
    parent.mkdir()
    parent.chmod(0o750)
    creation = options.pop("creation", Creation())
    backend = cast(ProvisioningBackend, creation) if isinstance(creation, Provisioning) else cast(CreationSession, creation)
    api = TunnelAPI(backend, parent / "http.sock", options.pop("allowed_proxy_uid", os.getuid()),
                    ORIGIN, options.pop("allowed_operator", ("operator", "second")), qualified=qualified, **options)
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


def test_authenticated_policy_accepts_new_principals_with_scoped_concurrent_requests(tmp_path):
    async def scenario():
        async with running(tmp_path, creation=Provisioning(), principal_policy="authenticated",
                           allowed_operator=()) as (api, backend):
            async def create(principal):
                identity = {"X-Ninja-Principal": principal}
                status, headers, options = await exchange(api, headers=identity)
                assert status == 200 and options["principal"] == principal
                assert options["policy"]["principal_policy"] == "authenticated"
                auth = {**identity, "Cookie": headers["Set-Cookie"].split(";", 1)[0],
                        "X-CSRF-Token": options["csrf_token"], "Origin": ORIGIN,
                        "Content-Type": "application/json", "Idempotency-Key": "same-key"}
                payload = json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode()
                first = await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload)
                assert first[0] == 202
                assert await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload) == first
                return principal, first[2]["operation"]
            results = await asyncio.gather(create("new-user@example.test"), create("another-new-user"))
            assert len(backend.calls) == len(backend.manager.operations) == 2
            assert results[0][1]["id"] != results[1][1]["id"]
            for principal, operation in results:
                other = next(name for name, _ in results if name != principal)
                assert (await exchange(api, path="/api/tunnel-operations/" + operation["id"],
                                       headers={"X-Ninja-Principal": other}))[0] == 404
    asyncio.run(scenario())


@pytest.mark.parametrize("principal", ["", " ", "anonymous user", "user;admin", "a" * 129])
def test_authenticated_policy_requires_a_nonempty_valid_trusted_principal(tmp_path, principal):
    async def scenario():
        async with running(tmp_path, principal_policy="authenticated", allowed_operator=()) as (api, backend):
            assert (await exchange(api, headers={"X-Ninja-Principal": principal}))[0] == 403
            assert not backend.calls and not api._sessions
    asyncio.run(scenario())


def test_authenticated_policy_keeps_proxy_origin_and_principal_bound_session_boundaries(tmp_path):
    async def scenario():
        async with running(tmp_path, principal_policy="authenticated", allowed_operator=()) as (api, backend):
            auth = await authorization(api)
            for change in ({"X-Ninja-Principal": "a-new-user"}, {"Origin": "https://attacker.example.test"},
                           {"Host": "attacker.example.test"}):
                assert (await exchange(api, "POST", "/api/tunnels", headers={**auth, **change},
                                       body=json.dumps(INTENT).encode()))[0] == 403
            api.allowed_proxy_uid += 1
            assert (await exchange(api, headers={"X-Ninja-Principal": "a-new-user"}))[0] == 403
            assert not backend.calls
    asyncio.run(scenario())


def test_dynamic_domain_options_include_unenrolled_transit_nodes_and_allow_ready_transit_headend(tmp_path):
    class DomainProvisioning(Provisioning):
        headends = {"PE1": "10.255.0.1", "P1": "10.255.0.2", "P2": "10.255.0.3"}
        allowed_destinations = ("PE1", "P1", "P2", "PE3")

        def readiness(self, headend=None):
            ready = headend != "P2"
            return ready, "Provisioning ready." if ready else "No pinned transport identity is enrolled."

        def node_readiness(self):
            return {name: {"transport_enrolled": name != "P2", "pcc_ready": True,
                           "credential_env": "PRIVATE_CREDENTIAL", "management_endpoint": "private.example.test"}
                    for name in self.headends}

    async def scenario():
        async with running(tmp_path, creation=DomainProvisioning()) as (api, backend):
            _, _, options = await exchange(api)
            assert options["headends"] == ["PE1", "P1", "P2"]
            assert options["destinations"] == ["PE1", "P1", "P2", "PE3"]
            assert options["headend_readiness"]["P2"] == {
                "enabled": False, "reason": "No pinned transport identity is enrolled.",
                "transport_enrolled": False, "pcc_ready": True}
            intent = {**INTENT, "headend": "P1"}
            auth = await authorization(api)
            assert (await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                   body=json.dumps(intent).encode()))[0] == 200
            assert backend.previews == [("operator", intent)]
            assert (await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                   body=json.dumps({**intent, "headend": "P2"}).encode()))[0] == 503
            backend.headends = {"P3": "10.255.0.4"}
            backend.allowed_destinations = ("PE1", "P3")
            _, _, updated = await exchange(api)
            assert updated["headends"] == ["P3"] and updated["destinations"] == ["PE1", "P3"]
    asyncio.run(scenario())


def test_unavailable_dynamic_domain_fails_closed_without_consuming_a_browser_session(tmp_path):
    class StaleDomain(Provisioning):
        @property
        def headends(self):
            raise ValueError("Trusted topology is stale")

    async def scenario():
        async with running(tmp_path, creation=StaleDomain()) as (api, backend):
            status, _, response = await exchange(api)
            assert status == 503 and "unavailable" in response["error"]
            assert not api._sessions and not backend.calls
    asyncio.run(scenario())


def test_configuration_retry_recovers_original_operation_after_domain_and_bandwidth_policy_change(tmp_path):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            auth = await authorization(api)
            payload = json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode()
            original = await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload)
            backend.manager.headends = {"P2": "10.255.0.3"}
            backend.manager.allowed_destinations = ("PE4",)
            backend.manager.max_bandwidth_bps = 500_000
            assert await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload) == original
            auth["Idempotency-Key"] = "new-key"
            assert (await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload))[0] == 400
            assert len(backend.admissions) == len(backend.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize(("mode", "operators"), [("unknown", ()), ([], ()), ("allowlist", ()),
                                                ("authenticated", ("operator",)),
                                                ("allowlist", ("operator", "operator")),
                                                ("authenticated", ([],))])
def test_principal_policy_configuration_is_explicit_and_unambiguous(tmp_path, mode, operators):
    with pytest.raises(ValueError):
        TunnelAPI(cast(CreationSession, Creation()), tmp_path / "api.sock", os.getuid(), ORIGIN,
                  operators, principal_policy=mode)


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


def test_qualified_configuration_preview_binds_creation_and_reports_only_independent_rsvp_checks(tmp_path):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            _, _, options = await exchange(api)
            assert options["backend"] == "router-config" and options["requires_plan"] is True
            assert options["policy"]["max_outstanding"] == 2
            assert options["policy"]["forwarding_verification"] == "unverified"
            auth = await authorization(api)
            del auth["Idempotency-Key"]
            status, _, response = await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                                  body=json.dumps(INTENT).encode())
            assert status == 200 and response["plan"]["plan_id"] == "e" * 64
            assert "interface Tunnel200" in response["plan"]["rendered_config"]
            assert backend.previews == [("operator", INTENT)]
            assert not backend.calls and not backend.manager.operations
            auth["Idempotency-Key"] = "approved-one"
            payload = {**INTENT, "approved_plan_id": response["plan"]["plan_id"]}
            status, _, result = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                                body=json.dumps(payload).encode())
            assert status == 202 and result["operation"]["status"] == "VERIFIED"
            assert result["operation"]["verification"]["scope"] == "headend-rsvp"
            assert backend.calls == [("operator", "approved-one", INTENT)]
            assert backend.approved_plans == ["e" * 64]
            path = "/api/tunnel-operations/" + result["operation"]["id"]
            assert (await exchange(api, path=path))[0] == 200
            assert (await exchange(api, path=path, headers={"X-Ninja-Principal": "second"}))[0] == 404
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["origin", "csrf", "principal", "cookie"])
def test_preview_has_the_same_authenticated_origin_session_and_csrf_boundary(tmp_path, changed):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            auth = await authorization(api)
            if changed == "origin":
                auth["Origin"] = "https://attacker.example.test"
            elif changed == "csrf":
                auth["X-CSRF-Token"] = "x" * 43
            elif changed == "principal":
                auth["X-Ninja-Principal"] = "second"
            else:
                del auth["Cookie"]
            status, _, _ = await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                          body=json.dumps(INTENT).encode())
            assert status == 403 and not backend.previews and not backend.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unqualified", "unavailable", "extra_cli", "extra_plan"])
def test_preview_cannot_bypass_backend_qualification_readiness_or_exact_intent(tmp_path, kind):
    async def scenario():
        async with running(tmp_path, creation=Provisioning(), qualified=kind != "unqualified") as (api, backend):
            if kind == "unavailable":
                backend.connection_ready = False
            payload = dict(INTENT)
            if kind == "extra_cli":
                payload["cli"] = "reload"
            elif kind == "extra_plan":
                payload["approved_plan_id"] = "e" * 64
            status, _, _ = await exchange(api, "POST", "/api/tunnel-plans", headers=await authorization(api),
                                          body=json.dumps(payload).encode())
            assert status == (400 if kind.startswith("extra") else 503)
            assert not backend.previews and not backend.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("plan_id", [None, "e" * 32, "E" * 64, "e" * 63, 123, "f" * 64])
def test_configuration_creation_requires_the_exact_approved_generated_plan(tmp_path, plan_id):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            payload = dict(INTENT)
            if plan_id is not None:
                payload["approved_plan_id"] = plan_id
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=await authorization(api),
                                          body=json.dumps(payload).encode())
            assert status == (409 if plan_id == "f" * 64 else 400)
            assert not backend.calls and not backend.manager.operations
    asyncio.run(scenario())


@pytest.mark.parametrize("bad_proof", [{}, {"independent": False},
                                      {"independent": True, "scope": "forwarding", "checks": []},
                                      {"independent": True, "scope": "headend-rsvp", "checks": ["bandwidth"]},
                                      {"independent": True, "scope": "headend-rsvp", "pcep_control": False,
                                       "checks": ["identity", "destination", "bandwidth", "ero", "signaling"]},
                                      {"independent": True, "scope": "headend-rsvp", "checks": [True]}])
def test_configuration_status_cannot_claim_verification_without_the_full_independent_rsvp_proof(tmp_path, bad_proof):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            _, _, result = await exchange(api, "POST", "/api/tunnels", headers=await authorization(api),
                                           body=json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode())
            backend.manager.operations[("operator", "request-one")]["verification"] = bad_proof
            status, _, _ = await exchange(api, path="/api/tunnel-operations/" + result["operation"]["id"])
            assert status == 503
    asyncio.run(scenario())


def test_native_creation_does_not_gain_a_preview_route_or_approve_arbitrary_plan_ids(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, backend):
            auth = await authorization(api)
            status, _, _ = await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                          body=json.dumps(INTENT).encode())
            assert status == 404
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                          body=json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode())
            assert status == 400 and not backend.calls
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["qualification", "backend", "topology"])
def test_configuration_lost_response_recovers_exact_existing_operation_without_write_authority(tmp_path, unavailable):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            auth = await authorization(api)
            body = json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode()
            first = await exchange(api, "POST", "/api/tunnels", headers=auth, body=body)
            assert first[0] == 202 and len(backend.calls) == 1
            if unavailable == "qualification":
                api.qualified = False
            elif unavailable == "backend":
                backend.connection_ready = False
            else:
                backend.manager.topology_ready = False
            assert (await exchange(api))[2]["enabled"] is False
            recovered = await exchange(api, "POST", "/api/tunnels", headers=auth, body=body)
            assert recovered == first and len(backend.calls) == 1
            # A new key cannot turn recovery into delivery authority. Qualified
            # configuration backends persist an admission rejection; an API
            # qualification withdrawal prevents even that backend invocation.
            auth["Idempotency-Key"] = "new-request"
            latest = await exchange(api, "POST", "/api/tunnels", headers=auth, body=body)
            if unavailable == "qualification":
                assert latest[0] == 503 and len(backend.admissions) == 1
            else:
                assert latest[0] == 202 and latest[2]["operation"]["status"] == "REJECTED"
                assert len(backend.admissions) == 2
            assert len(backend.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["intent", "plan"])
def test_configuration_recovery_conflict_precedes_readiness_and_never_calls_backend(tmp_path, changed):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            auth = await authorization(api)
            payload = {**INTENT, "approved_plan_id": "e" * 64}
            assert (await exchange(api, "POST", "/api/tunnels", headers=auth,
                                   body=json.dumps(payload).encode()))[0] == 202
            api.qualified = False
            backend.connection_ready = backend.manager.topology_ready = False
            if changed == "intent":
                payload["bandwidth_bps"] = 500_000
            else:
                payload["approved_plan_id"] = "f" * 64
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                          body=json.dumps(payload).encode())
            assert status == 409 and len(backend.calls) == 1
    asyncio.run(scenario())


def test_native_idempotent_replay_retains_the_existing_readiness_gate(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, backend):
            auth = await authorization(api)
            body = json.dumps(INTENT).encode()
            assert (await exchange(api, "POST", "/api/tunnels", headers=auth, body=body))[0] == 202
            api.qualified = False
            assert (await exchange(api, "POST", "/api/tunnels", headers=auth, body=body))[0] == 503
            assert len(backend.calls) == 1
    asyncio.run(scenario())


def test_configuration_durable_rejection_is_a_recoverable_operation_not_an_unknown_http_error(tmp_path):
    class RejectOnce(Provisioning):
        async def create(self, principal, key, intent, *, approved_plan_id):
            result = await super().create(principal, key, intent, approved_plan_id=approved_plan_id)
            if len(self.calls) == 1:
                result.update(status="REJECTED", verified=False, authorization=None, tunnel_id=None,
                              reason="Reviewed plan expired before delivery")
                result.pop("verification", None)
                self.manager.operations[(principal, key)] = copy.deepcopy(result)
            return result

    async def scenario():
        async with running(tmp_path, creation=RejectOnce()) as (api, backend):
            auth = await authorization(api)
            payload = {**INTENT, "approved_plan_id": "e" * 64}
            first = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(payload).encode())
            assert first[0] == 202 and first[2]["operation"]["status"] == "REJECTED"
            api.qualified = False
            assert await exchange(api, "POST", "/api/tunnels", headers=auth,
                                  body=json.dumps(payload).encode()) == first
            assert len(backend.calls) == 1
            api.qualified = True
            assert (await exchange(api, "POST", "/api/tunnel-plans", headers=auth,
                                   body=json.dumps(INTENT).encode()))[0] == 200
            auth["Idempotency-Key"] = "fresh-approved-request"
            status, _, result = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                                body=json.dumps(payload).encode())
            assert status == 202 and result["operation"]["status"] == "VERIFIED"
            assert len(backend.calls) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("unavailable", ["backend", "topology"])
def test_first_configuration_post_persists_readiness_rejection_and_recovers_it_without_new_admission(tmp_path, unavailable):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, backend):
            auth = await authorization(api)
            payload = json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode()
            if unavailable == "backend":
                backend.connection_ready = False
            else:
                backend.manager.topology_ready = False
            first = await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload)
            assert first[0] == 202 and first[2]["operation"]["status"] == "REJECTED"
            assert backend.admissions == [("operator", "request-one")] and not backend.calls
            api.qualified = False
            assert await exchange(api, "POST", "/api/tunnels", headers=auth, body=payload) == first
            assert backend.admissions == [("operator", "request-one")] and not backend.calls
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=auth,
                                          body=json.dumps({**INTENT, "bandwidth_bps": 500_000,
                                                           "approved_plan_id": "e" * 64}).encode())
            assert status == 409 and len(backend.admissions) == 1
    asyncio.run(scenario())


def test_api_qualification_withdrawal_blocks_unknown_configuration_key_before_backend_admission(tmp_path):
    async def scenario():
        async with running(tmp_path, creation=Provisioning(), qualified=False) as (api, backend):
            payload = json.dumps({**INTENT, "approved_plan_id": "e" * 64}).encode()
            status, _, _ = await exchange(api, "POST", "/api/tunnels", headers=await authorization(api), body=payload)
            assert status == 503
            assert not backend.admissions and not backend.calls and not backend.manager.operations
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
