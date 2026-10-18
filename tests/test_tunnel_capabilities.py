"""Capability evidence never replaces private provisioning authorization."""
from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from test_admin_http import admin_http as admin_http
from test_cli_provisioning import INTENT, controller
from test_dashboard import _request
from test_dashboard import dashboard_http as dashboard_http
from test_tunnel_api import Provisioning, authorization, exchange, running

from ninja_pce.state import load_state, write_state
from ninja_pce.tunnel_capabilities import (
    GLOBAL_CHECKS,
    HEADEND_CHECKS,
    capability_report,
    check,
    observer_options,
)


def passed(keys):
    return {key: check("pass", "TEST_PREREQUISITE_PASS", "Current prerequisite observed.", "Review exact approval.") for key in keys}


def qualified_report():
    return capability_report(passed(GLOBAL_CHECKS), {"PE1": {"checks": passed(HEADEND_CHECKS)}},
                             scope="existing-qualified-profile", enabled=True)


def files(root):
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def test_qualified_profile_never_becomes_production_ownership_or_forwarding_proof():
    report = qualified_report()
    assert report["available"] and report["headends"]["PE1"]["available"]
    assert report["informational"] and report["submission_revalidated"]
    assert not report["production_available"] and not report["control_ownership_verified"]
    assert not report["control_authority_verified"] and not report["forwarding_verified"] and not report["complete"]
    ownership = report["checks"]["exclusive_controller_ownership"]
    assert ownership["state"] == "unknown" and ownership["blocking_scopes"] == ["production"]
    assert ownership["code"] == "CONTROL_OWNERSHIP_UNVERIFIED"


@pytest.mark.parametrize("scope", ["observe", "standalone", "demo", "unrecognized"])
def test_other_scopes_never_enable_creation_even_with_passing_network_checks(scope):
    report = capability_report(passed(GLOBAL_CHECKS), {"PE1": {"checks": passed(HEADEND_CHECKS)}}, scope=scope, enabled=True)
    assert not report["available"] and not report["headends"]["PE1"]["available"]


@pytest.mark.parametrize("name", GLOBAL_CHECKS)
@pytest.mark.parametrize("state", ["fail", "unknown", "missing"])
def test_every_global_prerequisite_is_mandatory(name, state):
    checks = passed(GLOBAL_CHECKS)
    if state == "missing":
        del checks[name]
    else:
        checks[name]["state"] = state
        checks[name]["required"] = False  # Projection cannot make a mandatory check optional.
    report = capability_report(checks, {"PE1": {"checks": passed(HEADEND_CHECKS)}}, scope="existing-qualified-profile", enabled=True)
    assert not report["available"] and report["checks"][name]["required"] is True


@pytest.mark.parametrize("name", HEADEND_CHECKS)
def test_a_failed_selected_headend_does_not_hide_other_eligible_nodes(name):
    heads = {"PE1": {"checks": passed(HEADEND_CHECKS)}, "P1": {"checks": passed(HEADEND_CHECKS)}}
    heads["P1"]["checks"][name]["state"] = "unknown"
    report = capability_report(passed(GLOBAL_CHECKS), heads, scope="existing-qualified-profile", enabled=True)
    assert report["available"] and report["headends"]["PE1"]["available"]
    assert not report["headends"]["P1"]["available"]


def test_report_is_bounded_detached_and_cannot_override_ownership_unknown():
    checks = passed(GLOBAL_CHECKS)
    checks["exclusive_controller_ownership"] = check("pass", "FALSE_OWNERSHIP_CLAIM", "Claimed ownership", "Ignore")
    heads = {f"N{number}": {"checks": passed(HEADEND_CHECKS)} for number in range(300)}
    original = copy.deepcopy(heads)
    report = capability_report(checks, heads, scope="existing-qualified-profile", enabled=True)
    assert len(report["headends"]) == 256 and not report["available"]
    assert report["checks"]["endpoint_limit"]["state"] == "fail"
    assert report["checks"]["exclusive_controller_ownership"]["state"] == "unknown"
    assert heads == original
    report["headends"]["N0"]["checks"]["domain_eligible"]["state"] = "fail"
    assert heads == original


def test_healthy_authenticated_observer_stays_read_only():
    snapshot = {"mode": "live", "operating_mode": "observe", "topology_complete": True,
                "sources": [{"id": "topology", "state": "fresh"}, {"id": "bgpls", "state": "fresh"}],
                "peers": [{"protocol": "PCEP", "state": "ESTABLISHED", "fresh": True, "synchronized": True}],
                "lsps": [{"fresh": True, "delegated": True}]}
    result = observer_options(snapshot, authenticated=True)
    assert not result["enabled"] and result["policy"]["read_only"]
    report = result["capability"]
    assert report["checks"]["topology_ready"]["state"] == "pass"
    assert report["checks"]["authenticated_operator"]["state"] == "pass"
    assert report["checks"]["pcep_authorized"]["state"] == "fail"
    assert report["checks"]["provisioning_service_ready"]["state"] == "fail"
    assert not report["available"] and not report["production_available"]


def test_excessive_check_coverage_cannot_hide_a_required_refusal():
    checks = {**passed(GLOBAL_CHECKS), **passed([f"extra_{number}" for number in range(50)])}
    report = capability_report(checks, {"PE1": {"checks": passed(HEADEND_CHECKS)}},
                               scope="existing-qualified-profile", enabled=True)
    assert not report["available"] and not report["complete"]
    assert len(report["checks"]) <= 32
    assert report["checks"]["check_limit"]["state"] == "unknown"


def test_standalone_http_explains_missing_backend_without_adding_write_routes(dashboard_http):
    status, headers, body = _request(dashboard_http, "/api/tunnel-options")
    options = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert not options["capability"]["available"]
    assert options["capability"]["checks"]["provisioning_service_ready"]["code"] == "PROVISIONING_SERVICE_NOT_CONNECTED"
    assert _request(dashboard_http, "/api/tunnels", method="POST")[0] == 405


def test_admin_capability_is_authenticated_but_cannot_bridge_to_provisioning(admin_http):
    assert admin_http.request("/api/tunnel-options")[0] == 401
    admin_http.claim()
    before = files(admin_http.root)
    status, result, _ = admin_http.request("/api/tunnel-options")
    assert status == 200 and not result["enabled"]
    assert result["capability"]["checks"]["authenticated_operator"]["state"] == "pass"
    assert result["capability"]["checks"]["pcep_authorized"]["state"] == "fail"
    assert admin_http.request("/api/tunnels", method="POST", body=INTENT)[0] == 404
    assert files(admin_http.root) == before


def test_real_backend_options_are_one_read_only_current_observation(tmp_path, monkeypatch):
    backend = controller(tmp_path, qualified=True)
    original = backend.capability_observations
    observed = []
    def sample():
        observed.append(True)
        return original()
    monkeypatch.setattr(backend, "capability_observations", sample)
    for name in ("readiness", "node_readiness", "preview", "create", "list"):
        monkeypatch.setattr(backend, name, lambda *args, **kwargs: pytest.fail("Capability GET invoked admission or legacy resampling"))
    before = files(tmp_path)
    async def scenario():
        async with running(tmp_path, creation=backend) as (api, _):
            status, headers, result = await exchange(api)
            assert status == 200 and headers["Cache-Control"] == "no-store"
            assert result["enabled"] and result["capability"]["available"]
            assert result["capability"]["headends"]["PE1"]["available"]
            assert not result["capability"]["production_available"]
            assert result["headend_readiness"]["PE1"]["transport_enrolled"]
            capture = load_state(backend.pcep_capture)
            capture["generated_at"] = time.time() - 60
            write_state(backend.pcep_capture, capture)
            status, _, result = await exchange(api)
            assert status == 200 and not result["enabled"] and not result["capability"]["available"]
            assert result["capability"]["checks"]["pcep_listener_ready"]["state"] == "unknown"
    asyncio.run(scenario())
    assert observed == [True, True]
    after = files(tmp_path)
    assert {path: value for path, value in before.items() if path != backend.pcep_capture} == {
        path: value for path, value in after.items() if path != backend.pcep_capture}


def test_unauthorized_proxy_requests_do_not_sample_backend_capabilities(tmp_path, monkeypatch):
    backend = controller(tmp_path, qualified=True)
    monkeypatch.setattr(backend, "capability_observations", lambda: pytest.fail("Sampled unauthenticated principal"))
    async def scenario():
        async with running(tmp_path, creation=backend) as (api, _):
            assert (await exchange(api, headers={"X-Ninja-Principal": "viewer"}))[0] == 403
            assert (await exchange(api, headers={"Host": "attacker.example.test"}))[0] == 403
            assert not api._sessions
    asyncio.run(scenario())


def test_structured_observation_failure_stays_explanatory_and_never_cached(tmp_path, monkeypatch):
    backend = controller(tmp_path, qualified=True)
    async def scenario():
        async with running(tmp_path, creation=backend) as (api, _):
            assert (await exchange(api))[2]["capability"]["available"]
            def fail():
                raise ValueError("SECRET credential or private payload")
            monkeypatch.setattr(backend, "capability_observations", fail)
            status, _, result = await exchange(api)
            assert status == 200 and not result["enabled"] and not result["capability"]["available"]
            assert not result["headends"] and "SECRET" not in json.dumps(result)
    asyncio.run(scenario())


def test_a_capability_report_cannot_override_current_backend_admission(tmp_path, monkeypatch):
    backend = controller(tmp_path, qualified=True, connect_handler=lambda **kwargs: pytest.fail("Contacted router after failed admission"))
    original = backend.capability_observations()
    async def scenario():
        async with running(tmp_path, creation=backend) as (api, _):
            auth = await authorization(api)
            status, _, result = await exchange(api, "POST", "/api/tunnel-plans", headers=auth, body=json.dumps(INTENT).encode())
            assert status == 200
            payload = {**INTENT, "approved_plan_id": result["plan"]["plan_id"]}
            capture = load_state(backend.pcep_capture)
            for peer in capture["peers"].values():
                peer["synchronized"] = False
            write_state(backend.pcep_capture, capture)
            monkeypatch.setattr(backend, "capability_observations", lambda: copy.deepcopy(original))
            assert (await exchange(api))[2]["capability"]["available"]  # Informational projection cannot authorize admission.
            status, _, result = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(payload).encode())
            assert status == 202 and result["operation"]["status"] == "REJECTED"
            identifier = result["operation"]["id"]
            api.qualified = False
            status, _, recovered = await exchange(api, "POST", "/api/tunnels", headers=auth, body=json.dumps(payload).encode())
            assert status == 202 and recovered["operation"]["id"] == identifier
            assert recovered["operation"]["status"] == "REJECTED"
    asyncio.run(scenario())


def test_legacy_backend_tuple_readiness_does_not_invent_structured_capability(tmp_path):
    async def scenario():
        async with running(tmp_path, creation=Provisioning()) as (api, _):
            status, _, result = await exchange(api)
            assert status == 200 and result["enabled"]  # Existing API compatibility.
            assert not result["capability"]["available"]
            assert result["capability"]["checks"]["pcep_authorized"]["state"] == "unknown"
    asyncio.run(scenario())


@pytest.mark.parametrize("destinations", [[], ["PE1"]])
def test_no_distinct_authorized_endpoint_pair_never_advertises_availability(tmp_path, destinations):
    backend = controller(tmp_path, qualified=True)
    backend._allowed_destinations = tuple(destinations)
    async def scenario():
        async with running(tmp_path, creation=backend) as (api, _):
            status, _, result = await exchange(api)
            assert status == 200 and not result["enabled"] and not result["capability"]["available"]
            node = result["capability"]["headends"]["PE1"]
            assert not node["available"]
            assert node["checks"]["destination_available"]["state"] == "fail"
            assert node["checks"]["destination_available"]["code"] == "DISTINCT_DESTINATION_UNAVAILABLE"
    asyncio.run(scenario())
