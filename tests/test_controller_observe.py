"""Observe mode must preserve management access and prohibit PCEP startup."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from test_controller import config_file as config_file
from test_controller import save_config, synthetic_snapshot

from ninja_pce import controller
from ninja_pce.dashboard import DashboardStore
from ninja_pce.readiness import evaluate_readiness
from ninja_pce.state import load_state


def observe_config(config_file):
    path, document = config_file
    document["operating_mode"] = "observe"
    return controller.load_controller_config(save_config(config_file)), document


def test_observe_accepts_no_pcep_configuration_or_policy(config_file):
    path, document = config_file
    document["operating_mode"] = "observe"
    del document["pcep"]
    (path.parent / "domain.json").unlink()
    config = controller.load_controller_config(save_config(config_file))
    assert list(controller.component_commands(config)) == ["bgpls", "dashboard"]
    assert controller.effective_operating_mode(config) == "observe"
    assert not (path.parent / "state").exists()
    for components in (["pcep"], ["bgpls", "pcep", "dashboard"]):
        with pytest.raises(ValueError, match="PCEP is prohibited"):
            controller.component_commands(config, components)
    with pytest.raises(ValueError, match="PCEP is prohibited"):
        asyncio.run(controller._supervise(config, {"pcep": [sys.executable, "-c", "pass"]}))
    assert not Path(config.document["runtime_dir"]).exists()


@pytest.mark.parametrize("mode", ["pcep-monitor", "provisioning", "", False, 1, None])
def test_unimplemented_modes_do_not_alias_active_control(config_file, mode):
    _, document = config_file
    document["operating_mode"] = mode
    with pytest.raises(ValueError, match="operating_mode"):
        controller.load_controller_config(save_config(config_file))


@pytest.mark.parametrize("field", ["provisioning_state", "managed_recovery", "failover"])
def test_observe_rejects_authority_journal_settings_without_reading_or_rewriting_them(config_file, field):
    path, document = config_file
    document["operating_mode"] = "observe"
    protected = path.parent / "existing-journal.json"
    protected.write_bytes(b"existing ownership and unresolved recovery history")
    document["pcep"][field] = str(protected)
    with pytest.raises(ValueError, match="must omit active"):
        controller.load_controller_config(save_config(config_file))
    assert protected.read_bytes() == b"existing ownership and unresolved recovery history"
    assert not (path.parent / "state").exists()


def test_inactive_policy_cannot_alias_collector_output(config_file):
    _, document = config_file
    document["operating_mode"] = "observe"
    document["pcep"]["domain_policy"] = document["files"]["bgpls_raw"]
    with pytest.raises(ValueError, match="distinct"):
        controller.load_controller_config(save_config(config_file))


def test_legacy_mode_and_canonical_document_remain_implicit(config_file):
    path, _ = config_file
    before = path.read_bytes()
    config = controller.load_controller_config(path)
    assert controller.effective_operating_mode(config) == "pcep-control"
    assert "operating_mode" not in config.document
    assert list(controller.component_commands(config)) == ["bgpls", "pcep", "dashboard"]
    assert path.read_bytes() == before


def test_observer_with_fresh_topology_and_zero_pccs_is_healthy(config_file, monkeypatch):
    config, _ = observe_config(config_file)
    monkeypatch.setattr(DashboardStore, "snapshot", lambda *args: synthetic_snapshot(pccs=0))
    monkeypatch.setattr(controller, "_runtime_status",
                        lambda config, component: {"component": component, "state": "running"})
    status, code = controller.controller_status(config)
    assert code == 0 and status["status"] == "healthy"
    assert status["readiness"]["state"] == "READY_OBSERVING"
    assert status["protocol"]["fresh_synchronized_pccs"] == 0
    assert [item["component"] for item in status["processes"]] == ["bgpls", "dashboard"]
    assert not status["readiness"]["control_authority_verified"]
    assert not status["forwarding_verified"]


@pytest.mark.parametrize("problem,expected", [
    ("missing", "WAITING_FOR_BGPLS"), ("stale", "TOPOLOGY_STALE"), ("invalid", "TOPOLOGY_INCOMPLETE"),
    ("exclusions", "TOPOLOGY_INCOMPLETE"), ("empty", "TOPOLOGY_INCOMPLETE"),
    ("collector", "BGPLS_UNAVAILABLE"), ("management", "MANAGEMENT_UNAVAILABLE"),
    ("config", "CONFIGURATION_MISMATCH"),
])
def test_observer_readiness_fails_closed_for_required_evidence(problem, expected):
    snapshot = synthetic_snapshot(pccs=0)
    processes = [{"component": item, "state": "running"} for item in ("bgpls", "dashboard")]
    if problem in {"missing", "stale", "invalid"}:
        snapshot["sources"][0]["state"] = "unavailable" if problem == "missing" else problem
    elif problem == "exclusions":
        snapshot["topology_complete"] = False
    elif problem == "empty":
        snapshot["summary"]["nodes"] = 0
    elif problem == "collector":
        processes[0]["state"] = "failed"
    elif problem == "management":
        processes[1]["state"] = "stopped"
    else:
        processes[0]["state"] = "config_mismatch"
    readiness = evaluate_readiness(snapshot, "observe", processes)
    assert readiness["state"] == expected and not readiness["ready"]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.parametrize("fault", ["exit", "spawn", "rpc"])
def test_management_http_survives_protocol_failure_without_pcep_listener(config_file, fault):
    path, document = config_file
    document["operating_mode"] = "observe"
    dashboard_port, pcep_port = free_port(), free_port()
    while pcep_port == dashboard_port:
        pcep_port = free_port()
    document["dashboard"]["port"] = dashboard_port
    document["pcep"].update(listen_host="127.0.0.1", port=pcep_port)
    document["bgpls"]["grpcurl"] = "ninja-pce-test-missing-grpcurl"
    config = controller.load_controller_config(save_config(config_file))
    state = Path(config.document["state_dir"])
    state.mkdir()
    journal = state / "managed-recovery.json"
    journal.write_bytes(b"retained LSP authority and reservation history")
    script = """
import asyncio, sys
from ninja_pce.controller import load_controller_config, component_commands, _supervise
config = load_controller_config(sys.argv[1])
commands = component_commands(config)
if sys.argv[2] == 'exit':
    commands['bgpls'] = [sys.executable, '-c', 'import time,sys;time.sleep(.2);sys.exit(7)']
elif sys.argv[2] == 'spawn':
    commands['bgpls'] = ['/nonexistent-ninja-pce-test-collector']
raise SystemExit(asyncio.run(_supervise(config, commands)))
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(path), fault],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f"http://127.0.0.1:{dashboard_port}"
    try:
        deadline = time.monotonic() + 12
        snapshot = None
        while time.monotonic() < deadline:
            assert process.poll() is None, "Management supervisor exited with its protocol child"
            try:
                with client.open(origin + "/api/controller", timeout=1) as response:
                    snapshot = json.load(response)
                expected = "BGPLS_UNAVAILABLE"
                fault_persisted = load_state(controller._runtime_path(config, "bgpls"))["state"] == "failed"
                if snapshot["readiness"]["state"] == expected and fault_persisted:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(.03)
        assert snapshot is not None and snapshot["readiness"]["state"] == expected
        with client.open(origin + "/healthz", timeout=2) as response:
            assert response.status == 200 and json.load(response)["status"] == "ok"
        with pytest.raises(urllib.error.HTTPError) as unavailable:
            client.open(origin + "/readyz", timeout=2)
        assert unavailable.value.code == 503
        assert not controller._runtime_path(config, "pcep").exists()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", pcep_port), timeout=.3)
        if fault != "rpc":
            record = load_state(controller._runtime_path(config, "bgpls"))
            assert record["state"] == "failed"
            if fault == "exit":
                assert record["returncode"] == 7
            else:
                assert "startup failed" in record["error"]
        assert journal.read_bytes() == b"retained LSP authority and reservation history"
        process.terminate()
        assert process.wait(timeout=8) == 0
        dashboard_record = load_state(controller._runtime_path(config, "dashboard"))
        assert controller._process_identity(dashboard_record["child"]["pid"]) is None
        if fault != "rpc":
            assert load_state(controller._runtime_path(config, "bgpls"))["state"] == "failed"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
