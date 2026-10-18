"""Read-only backend prerequisites never admit requests or expose local state."""
from __future__ import annotations

import copy
import json
import os
import re
import time

import pytest
from test_cli_provisioning import controller, multi_area_controller, multi_area_runtime
from test_domain_provisioning import domain_controller
from test_tunnel_creation import PEER

from ninja_pce import cli_provisioning
from ninja_pce.models import ManagedDevice
from ninja_pce.state import load_state, write_state

GLOBAL = {"backend_qualified", "writes_enabled", "topology_ready", "pcep_listener_ready",
          "pcep_authorized", "recovery_clear", "topology_policy_matches"}
LOCAL = {"domain_eligible", "transport_enrolled", "transport_ready", "synchronized_headend"}
CAPTURE = {"pcep_listener_ready", "pcep_authorized", "recovery_clear", "topology_policy_matches"}


def state(report, name, headend=None):
    checks = report["checks"] if headend is None else report["headends"][headend]["checks"]
    return checks[name]["state"]


def files(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def test_qualified_prerequisites_are_current_detached_and_redacted(tmp_path, monkeypatch):
    monkeypatch.delenv("NINJA_PCE_PREVIEW_ONLY", raising=False)
    backend = controller(tmp_path, qualified=True)
    before = files(tmp_path)
    report = backend.capability_observations()
    assert set(report) == {"checks", "headends", "headend_addresses", "destinations"}
    assert set(report["checks"]) == GLOBAL
    assert set(report["headends"]["PE1"]["checks"]) == LOCAL
    assert {item["state"] for item in report["checks"].values()} == {"pass"}
    assert {item["state"] for item in report["headends"]["PE1"]["checks"].values()} == {"pass"}
    assert report["headend_addresses"] == {"PE1": PEER}
    assert report["destinations"] == ["PE3"]
    for item in [*report["checks"].values(), *report["headends"]["PE1"]["checks"].values()]:
        assert set(item) == {"state", "code", "message", "next_action"}
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", item["code"])
        assert item["message"] and item["next_action"]
    encoded = json.dumps(report)
    for secret in (str(tmp_path), "NINJA_PCE_TEST_PASSWORD", "password_env", "fingerprint", "checksum"):
        assert secret not in encoded
    report["headend_addresses"].clear()
    report["destinations"].clear()
    assert backend.capability_observations()["headend_addresses"] == {"PE1": PEER}
    assert files(tmp_path) == before
    assert backend.readiness("PE1") == (True, "")


def test_one_topology_and_capture_sample_without_admission_locks_or_writes(tmp_path, monkeypatch):
    backend = controller(tmp_path, qualified=True)
    original_snapshot = backend.computer.validated_snapshot
    original_capture = cli_provisioning._capability_capture
    calls = []

    def snapshot():
        calls.append("topology")
        return original_snapshot()

    def capture(path):
        calls.append("capture")
        return original_capture(path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Capabilities must not admit, lock, write, or contact a router")

    monkeypatch.setattr(backend.computer, "validated_snapshot", snapshot)
    monkeypatch.setattr(cli_provisioning, "_capability_capture", capture)
    for name in ("resource_lock", "write_state", "push_plan", "verify_plan"):
        monkeypatch.setattr(cli_provisioning, name, forbidden)
    for name in ("readiness", "preview", "create", "_load", "list", "_transport_revision"):
        monkeypatch.setattr(backend, name, forbidden)
    before = files(tmp_path)
    assert state(backend.capability_observations(), "topology_ready") == "pass"
    assert calls == ["topology", "capture"]
    assert files(tmp_path) == before


@pytest.mark.parametrize(("problem", "check"), [
    ("unqualified", "backend_qualified"), ("preview_only", "writes_enabled"),
    ("listener_stopped", "pcep_listener_ready"), ("capture_error", "pcep_listener_ready"),
    ("capability_disabled", "pcep_authorized"), ("authorization_changed", "pcep_authorized"),
    ("pending_recovery", "recovery_clear"), ("managed_pending", "recovery_clear"),
    ("managed_blocked", "recovery_clear"), ("managed_reconciliation", "recovery_clear"),
    ("unexpected_area_policy", "topology_policy_matches"),
])
def test_each_explicit_global_barrier_has_a_fixed_failure(tmp_path, monkeypatch, problem, check):
    monkeypatch.delenv("NINJA_PCE_PREVIEW_ONLY", raising=False)
    backend = controller(tmp_path, qualified=problem != "unqualified")
    capture = load_state(backend.pcep_capture)
    if problem == "preview_only":
        monkeypatch.setenv("NINJA_PCE_PREVIEW_ONLY", "true")
    elif problem == "listener_stopped":
        capture["listener_running"] = False
    elif problem == "capture_error":
        capture["capture_error"] = "private credential, raw payload and /private/path must be redacted"
    elif problem == "capability_disabled":
        capture["capabilities"]["configuration_provisioning"] = False
    elif problem == "authorization_changed":
        capture["provisioning_policy_digest"] = "private-policy-value"
    elif problem == "pending_recovery":
        capture["failure_recovery"] = {"pending": {"private": "raw journal"}}
    elif problem == "managed_pending":
        capture["managed_failure_recovery"] = {"pending": {"id": "private-operation"}}
    elif problem == "managed_blocked":
        capture["managed_failure_recovery"] = {"blocked": True}
    elif problem == "managed_reconciliation":
        capture["managed_failure_recovery"] = {"status": "RECOVERY_REQUIRED"}
    elif problem == "unexpected_area_policy":
        capture["topology_policy"] = None
    write_state(backend.pcep_capture, capture)
    before = files(tmp_path)
    report = backend.capability_observations()
    assert state(report, check) == "fail"
    assert report["checks"][check]["code"] == check.upper() + "_FAIL"
    assert "private" not in json.dumps(report)
    assert files(tmp_path) == before
    assert backend.readiness("PE1")[0] is False


@pytest.mark.parametrize(("key", "value"), [
    ("state", "CLOSED"), ("fresh", False), ("synchronized", False), ("valid_until", 0),
])
def test_disconnected_stale_unsynchronized_or_expired_pcc_is_a_failure(tmp_path, key, value):
    backend = controller(tmp_path, qualified=True)
    capture = load_state(backend.pcep_capture)
    capture["peers"][PEER][key] = value
    write_state(backend.pcep_capture, capture)
    assert state(backend.capability_observations(), "synchronized_headend", "PE1") == "fail"


@pytest.mark.parametrize("key", ["state", "fresh", "synchronized", "valid_until"])
def test_missing_required_pcc_evidence_is_unknown(tmp_path, key):
    backend = controller(tmp_path, qualified=True)
    capture = load_state(backend.pcep_capture)
    del capture["peers"][PEER][key]
    write_state(backend.pcep_capture, capture)
    assert state(backend.capability_observations(), "synchronized_headend", "PE1") == "unknown"


@pytest.mark.parametrize("generated", [None, True, "recent", [], 0, 10**200, 10**500, -(10**500)])
def test_missing_malformed_and_expired_capture_time_never_reuses_availability(tmp_path, generated):
    backend = controller(tmp_path, qualified=True)
    assert state(backend.capability_observations(), "pcep_listener_ready") == "pass"
    capture = load_state(backend.pcep_capture)
    capture["generated_at"] = generated
    write_state(backend.pcep_capture, capture)
    report = backend.capability_observations()
    assert all(state(report, name) == "unknown" for name in CAPTURE)
    assert state(report, "synchronized_headend", "PE1") == "unknown"
    assert state(report, "topology_ready") == "pass"


@pytest.mark.parametrize("until", [None, True, "future", [], 10**500, -(10**500)])
def test_malformed_pcc_validity_is_unknown_without_numeric_conversion_errors(tmp_path, until):
    backend = controller(tmp_path, qualified=True)
    capture = load_state(backend.pcep_capture)
    capture["peers"][PEER]["valid_until"] = until
    write_state(backend.pcep_capture, capture)
    assert state(backend.capability_observations(), "synchronized_headend", "PE1") == "unknown"


@pytest.mark.parametrize("problem", ["missing", "checksum", "json", "duplicate", "symlink", "fifo", "oversized"])
def test_unavailable_or_unsafe_capture_returns_unknown_without_reading_arbitrary_streams(tmp_path, problem):
    backend = controller(tmp_path, qualified=True)
    path = backend.pcep_capture
    if problem == "missing":
        path.unlink()
    elif problem == "checksum":
        document = json.loads(path.read_bytes())
        document["checksum"] = "0" * 64
        path.write_text(json.dumps(document))
    elif problem == "json":
        path.write_bytes(b"private raw invalid capture")
    elif problem == "duplicate":
        path.write_text('{"version":2,"version":2}')
    elif problem == "symlink":
        retained = path.with_suffix(".retained")
        path.rename(retained)
        path.symlink_to(retained)
    elif problem == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif problem == "oversized":
        with path.open("wb") as handle:
            handle.truncate(16_777_217)
    started = time.monotonic()
    report = backend.capability_observations()
    assert time.monotonic() - started < 1
    assert all(state(report, name) == "unknown" for name in CAPTURE)
    assert state(report, "synchronized_headend", "PE1") == "unknown"
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(("key", "value", "check"), [
    ("listener_running", None, "pcep_listener_ready"),
    ("capabilities", [], "pcep_authorized"),
    ("provisioning_policy_digest", None, "pcep_authorized"),
    ("peers", [], "pcep_authorized"),
    ("failure_recovery", [], "recovery_clear"),
    ("managed_failure_recovery", None, "recovery_clear"),
])
def test_malformed_required_observations_are_unknown_not_enabled(tmp_path, key, value, check):
    backend = controller(tmp_path, qualified=True)
    capture = load_state(backend.pcep_capture)
    capture[key] = value
    write_state(backend.pcep_capture, capture)
    assert state(backend.capability_observations(), check) == "unknown"


def test_topology_failure_discards_every_previously_selectable_endpoint(tmp_path):
    backend = controller(tmp_path, qualified=True)
    assert backend.capability_observations()["headend_addresses"]
    backend.computer.error = "private collector details and credentials"
    report = backend.capability_observations()
    assert state(report, "topology_ready") == "unknown"
    assert report["headends"] == report["headend_addresses"] == {}
    assert report["destinations"] == []
    assert "private" not in json.dumps(report)


def test_domain_membership_includes_unenrolled_transit_routers(tmp_path, monkeypatch):
    device = ManagedDevice("PE1", "PE1", "192.0.2.1", "cisco_ios", "ssh-user",
                           password_env="NINJA_TEST_TRANSPORT")
    backend = domain_controller(tmp_path, monkeypatch, devices={"PE1": device})
    report = backend.capability_observations()
    assert set(report["headend_addresses"]) == {"PE1", "P1", "PE3"}
    assert set(report["destinations"]) == {"PE1", "P1", "PE3"}
    assert state(report, "domain_eligible", "P1") == "pass"
    assert state(report, "transport_enrolled", "P1") == "fail"
    assert state(report, "transport_ready", "P1") == "fail"
    assert state(report, "synchronized_headend", "P1") == "pass"
    backend.computer.topology.raw_metadata["bgpls_objects"]["P1"]["instance_id"] = 88
    assert "P1" not in backend.capability_observations()["headend_addresses"]


@pytest.mark.parametrize("problem", ["credential", "pins"])
def test_transport_readiness_checks_do_not_expose_credentials_pins_or_management_profiles(tmp_path, monkeypatch, problem):
    backend = domain_controller(tmp_path, monkeypatch)
    if problem == "credential":
        monkeypatch.delenv("NINJA_TEST_TRANSPORT")
    else:
        (tmp_path / "known_hosts").write_text("private malformed host key\n")
    report = backend.capability_observations()
    assert state(report, "transport_enrolled", "PE1") == "pass"
    assert state(report, "transport_ready", "PE1") == "fail"
    encoded = json.dumps(report)
    for value in ("private", "synthetic-secret", "NINJA_TEST_TRANSPORT", "known_hosts", "192.0.2.1"):
        assert value not in encoded


@pytest.mark.parametrize("problem", ["match", "changed", "missing"])
def test_area_policy_matches_exact_current_listener_policy(tmp_path, monkeypatch, problem):
    config = multi_area_runtime(tmp_path, monkeypatch)
    backend = multi_area_controller(tmp_path, config, qualified=True)
    capture = load_state(backend.pcep_capture)
    if problem == "changed":
        capture["topology_policy"]["ospf_multi_area_digest"] = "private-different-policy"
    elif problem == "missing":
        del capture["topology_policy"]
    write_state(backend.pcep_capture, capture)
    before = copy.deepcopy(capture)
    report = backend.capability_observations()
    assert state(report, "topology_policy_matches") == {"match": "pass", "changed": "fail", "missing": "unknown"}[problem]
    assert load_state(backend.pcep_capture) == before
    assert "private" not in json.dumps(report)
