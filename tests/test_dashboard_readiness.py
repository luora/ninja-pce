"""Mode-specific readiness and browser truthfulness using local captured inputs."""
from __future__ import annotations

import base64
import copy
import http.client
import json
import os
import struct
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import test_dashboard_creation_ui as creation_ui
from test_bgpls import link, route, te_attrs
from test_controller import config_file as config_file
from test_dashboard import EXPORTER, NODE_NAMES, _publish, _source

from ninja_pce import controller, dashboard
from ninja_pce.bgpls import parse_gobgp_snapshot, topology_document
from ninja_pce.cli import main
from ninja_pce.dashboard import DashboardConfig, DashboardStore, create_dashboard_server
from ninja_pce.state import load_state, write_state


def test_observe_is_ready_without_reading_old_pcep_sessions_or_events(tmp_path, monkeypatch):
    config, now = _publish(tmp_path)
    config = replace(config, operating_mode="observe", required_bgpls_peers=(EXPORTER,))
    read = dashboard._read

    def checked_read(path, **options):
        assert path != config.pcep_path, "Observe mode must not read retained PCEP state"
        return read(path, **options)

    monkeypatch.setattr(dashboard, "_read", checked_read)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert snapshot["operating_mode"] == "observe"
    assert snapshot["readiness"]["state"] == "READY_OBSERVING"
    assert snapshot["readiness"]["ready"] is True
    assert _source(snapshot, "pcep")["state"] == "disabled"
    assert "intentionally disabled" in _source(snapshot, "pcep")["message"]
    assert snapshot["summary"]["pcep_peers"] == snapshot["summary"]["active_lsps"] == 0
    assert not any(peer["protocol"] == "PCEP" for peer in snapshot["peers"])
    assert snapshot["lsps"] == snapshot["events"] == snapshot["computations"] == []


def test_pcep_ready_requires_a_current_synchronized_pcc(tmp_path):
    config, now = _publish(tmp_path)
    store = DashboardStore(replace(config, required_bgpls_peers=(EXPORTER,)))
    assert store.snapshot(now=now)["readiness"]["state"] == "READY_PCEP"
    capture = load_state(config.pcep_path)
    capture["peers"] = {}
    write_state(config.pcep_path, capture)
    snapshot = store.snapshot(now=now)
    assert _source(snapshot, "pcep")["state"] == "fresh"
    assert snapshot["readiness"]["state"] == "WAITING_FOR_PCC"
    assert snapshot["readiness"]["ready"] is False


def test_observe_retains_readonly_ledger_without_creating_state(tmp_path):
    config, now = _publish(tmp_path)
    config = replace(config, operating_mode="observe")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    snapshot = DashboardStore(config).snapshot(now=now)
    assert snapshot["reservations"]["configured"] is False
    assert not config.state_dir.exists()
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_stale_topology_blocks_both_modes(tmp_path):
    config, now = _publish(tmp_path)
    for mode in ("observe", "pcep-control"):
        snapshot = DashboardStore(replace(config, operating_mode=mode)).snapshot(now=now + timedelta(seconds=31))
        assert snapshot["readiness"]["state"] == "TOPOLOGY_STALE"
        assert snapshot["readiness"]["ready"] is False


def test_configured_peer_identity_cannot_be_selected_by_observation_files(tmp_path):
    config, now = _publish(tmp_path)
    snapshot = DashboardStore(replace(config, operating_mode="observe",
                                      required_bgpls_peers=("192.0.2.200",))).snapshot(now=now)
    assert snapshot["readiness"]["ready"] is False
    assert snapshot["topology_complete"] is False
    assert _source(snapshot, "topology")["state"] == _source(snapshot, "bgpls")["state"] == "invalid"
    assert not any(peer["fresh"] for peer in snapshot["peers"])
    assert {peer["address"] for peer in snapshot["peers"]} == {"192.0.2.200"}


@pytest.mark.parametrize("phase", ["peers_before", "peers_after"])
def test_both_peer_snapshots_require_end_of_rib(tmp_path, phase):
    config, now = _publish(tmp_path)
    raw = json.loads(config.bgpls_raw_path.read_text())
    raw[phase][0]["peer"]["afiSafis"][0]["mpGracefulRestart"]["state"]["endOfRibReceived"] = False
    config.bgpls_raw_path.write_text(json.dumps(raw))
    snapshot = DashboardStore(replace(config, operating_mode="observe")).snapshot(now=now)
    assert snapshot["readiness"]["ready"] is False
    assert not any(peer["fresh"] for peer in snapshot["peers"])


@pytest.mark.parametrize("kind", [1, 2, 3, 4, 99])
def test_excluded_te_objects_block_readiness_but_prefixes_are_optional(tmp_path, kind):
    config, now = _publish(tmp_path)
    raw = json.loads(config.bgpls_raw_path.read_text())
    if kind == 1:
        unsupported = copy.deepcopy(next(item for item in raw["rib"] if
                                        base64.b64decode(item["destination"]["paths"][0]["nlriBinary"])[:2]
                                        == b"\x00\x01"))
        wire = bytearray(base64.b64decode(unsupported["destination"]["paths"][0]["nlriBinary"]))
        wire[4] = 99  # Unsupported node IGP protocol, retained as a diagnostic.
        unsupported["destination"]["paths"][0]["nlriBinary"] = base64.b64encode(wire).decode()
    elif kind == 2:
        attrs = te_attrs()
        del attrs[1092]  # Missing mandatory link TE metric.
        unsupported = route(link(local="192.0.2.111"), attrs)
    else:
        unsupported = route(struct.pack("!HH", kind, 9) + bytes([3]) + bytes(8), {})
    raw["rib"].append(unsupported)
    topology = parse_gobgp_snapshot(raw, required_peers=(EXPORTER,), node_names=NODE_NAMES, now=now)
    config.bgpls_raw_path.write_text(json.dumps(raw))
    config.topology_path.write_text(json.dumps(topology_document(topology)))
    write_state(config.bgpls_status_path, {
        "status": "ok", "observed_at": now.timestamp(), "nodes": len(topology.nodes),
        "directed_links": len(topology.links), "metadata": topology.raw_metadata,
    })
    snapshot = DashboardStore(replace(config, operating_mode="observe")).snapshot(now=now)
    assert _source(snapshot, "topology")["state"] == "fresh"
    assert snapshot["topology_complete"] is (kind in {3, 4})
    assert snapshot["readiness"]["ready"] is (kind in {3, 4})
    assert snapshot["readiness"]["state"] == ("READY_OBSERVING" if kind in {3, 4} else "TOPOLOGY_INCOMPLETE")


def test_managed_snapshot_checks_only_enabled_components(config_file, monkeypatch):
    path, document = config_file
    document["operating_mode"] = "observe"
    path.write_text(json.dumps(document))
    config = controller.load_controller_config(path)
    observed = []

    def runtime_status(_config, component):
        observed.append(component)
        return {"component": component, "state": "running"}

    monkeypatch.setattr(controller, "_runtime_status", runtime_status)
    snapshot = DashboardStore(DashboardConfig(
        operating_mode="observe", controller_config_path=path,
        controller_config_digest=config.config_digest,
    )).snapshot()
    assert set(observed) == {"bgpls", "dashboard"}
    assert snapshot["processes"] == [{"component": item, "state": "running"} for item in observed]


@pytest.mark.parametrize("change", ["config", "mode", "missing"])
def test_managed_snapshot_never_accepts_a_different_configuration(config_file, change):
    path, document = config_file
    managed = controller.load_controller_config(path)
    config = DashboardConfig(controller_config_path=path, controller_config_digest=managed.config_digest)
    if change == "config":
        document["dashboard"]["refresh_seconds"] = 8
        path.write_text(json.dumps(document))
    elif change == "mode":
        config = replace(config, operating_mode="observe")
    else:
        path.unlink()
    snapshot = DashboardStore(config).snapshot()
    assert snapshot["readiness"]["state"] == "CONFIGURATION_MISMATCH"
    assert snapshot["readiness"]["ready"] is False


def test_ready_endpoint_tracks_freshness_while_health_remains_liveness(tmp_path, monkeypatch):
    config, now = _publish(tmp_path)
    store_snapshot = DashboardStore.snapshot
    monkeypatch.setattr(DashboardStore, "snapshot", lambda self: store_snapshot(self, now=now))
    server = create_dashboard_server(replace(config, operating_mode="observe"))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/readyz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["state"] == "READY_OBSERVING"
        config.bgpls_raw_path.unlink()
        connection.request("GET", "/readyz")
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read())["ready"] is False
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"status": "ok", "read_only": True, "mode": "live"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_corrupt_nested_runtime_record_keeps_management_http_available(config_file):
    path, document = config_file
    document["operating_mode"] = "observe"
    path.write_text(json.dumps(document))
    managed = controller.load_controller_config(path)
    data = managed.document
    runtime_dir = Path(data["runtime_dir"])
    runtime_dir.mkdir(parents=True)
    identity = {"pid": os.getpid(), "start_ticks": controller._process_identity(os.getpid())}
    write_state(runtime_dir / ".ninja-pce-controller-dashboard.json", {
        "kind": "controller_component", "component": "dashboard", "state": "running",
        "config_digest": managed.config_digest, "updated_at": time.time(),
        "supervisor": identity, "child": identity,
    })
    corrupted = runtime_dir / ".ninja-pce-controller-bgpls.json"
    corrupted.write_text('{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}')
    before = corrupted.read_bytes(), corrupted.stat().st_mtime_ns
    config = DashboardConfig(
        topology_path=Path(data["files"]["topology"]),
        bgpls_status_path=Path(data["files"]["bgpls_status"]),
        bgpls_raw_path=Path(data["files"]["bgpls_raw"]),
        pcep_path=Path(data["files"]["pcep_capture"]), state_dir=Path(data["state_dir"]),
        operating_mode="observe", required_bgpls_peers=tuple(data["bgpls"]["required_peers"]),
        controller_config_path=path, controller_config_digest=managed.config_digest, port=0,
    )
    server = create_dashboard_server(config)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request("GET", "/api/controller")
        response = connection.getresponse()
        assert response.status == 200
        snapshot = json.loads(response.read())
        assert snapshot["readiness"]["state"] == "BGPLS_UNAVAILABLE"
        assert snapshot["readiness"]["scope"] == "runtime"
        assert {process["component"]: process["state"] for process in snapshot["processes"]} == {
            "bgpls": "unobserved", "dashboard": "running",
        }
        connection.request("GET", "/readyz")
        response = connection.getresponse()
        assert response.status == 503
        assert json.loads(response.read())["state"] == "BGPLS_UNAVAILABLE"
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["status"] == "ok"
        assert (corrupted.read_bytes(), corrupted.stat().st_mtime_ns) == before
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_demo_never_satisfies_ready_endpoint():
    snapshot = DashboardStore(DashboardConfig(demo=True)).snapshot()
    assert snapshot["mode"] == "demo"
    assert snapshot["readiness"]["ready"] is False


def test_cli_passes_mode_config_digest_and_explicit_peer_before_binding(tmp_path, monkeypatch):
    observed = []

    def bind(config):
        observed.append(config)
        raise OSError("Synthetic listener boundary")

    monkeypatch.setattr(dashboard, "create_dashboard_server", bind)
    assert main(["dashboard", "--operating-mode", "observe", "--bgpls-peer", EXPORTER,
                 "--controller-config", str(tmp_path / "controller.json"),
                 "--controller-digest", "a" * 64]) == 1
    assert observed[0].operating_mode == "observe"
    assert observed[0].required_bgpls_peers == (EXPORTER,)
    assert observed[0].controller_config_digest == "a" * 64


@pytest.mark.skipif(creation_ui.CHROME is None, reason="Optional headless Chromium is unavailable")
def test_browser_shows_mode_readiness_and_disabled_pcep_without_retaining_ready(tmp_path, monkeypatch):
    harness = r"""
    const pause=(ms)=>new Promise(resolve=>setTimeout(resolve,ms));
    const assert=(value,message)=>{if(!value)throw new Error(message);};
    const $test=id=>document.getElementById(id);
    let disconnected=false;
    window.fetch=async(path)=>{
      if(disconnected)throw new Error("Synthetic management outage");
      return {ok:true,status:200,json:async()=>({schema_version:1,read_only:true,mode:"live",
        operating_mode:"observe",readiness:{state:"READY_OBSERVING",ready:true,message:"Topology is current; PCEP is intentionally disabled."},
        sources:[{id:"pcep",label:"PCEP listener",state:"disabled",message:"PCEP is intentionally disabled in observe mode."}],
        lsps:[],topology:{nodes:[],links:[]},summary:{pcep_peers:0},peers:[],events:[],computations:[],refresh_seconds:60})};
    };
    """
    case = r"""
    assert(!$test("readiness-banner").hidden,"Readiness missing");
    assert($test("readiness-state").textContent==="Ready observing","Observation readiness missing");
    assert($test("operating-mode").textContent==="Observe","Operating mode missing");
    assert($test("stat-peers").textContent==="Disabled","Disabled PCEP appears unavailable");
    assert($test("stat-peer-detail").textContent.includes("Intentionally disabled"),"Disabled state unexplained");
    document.querySelector('[data-view="peers"]').click();
    assert($test("source-cards").textContent.includes("intentionally disabled"),"Source lacks intentional mode state");
    disconnected=true;
    $test("refresh-button").click();await pause(30);
    assert($test("readiness-state").textContent==="Readiness unavailable","Disconnected snapshot retained readiness");
    assert($test("info-readiness").textContent.includes("Current readiness unconfirmed"),"Info retained readiness");
    """
    monkeypatch.setattr(creation_ui, "HARNESS", harness)
    monkeypatch.setitem(creation_ui.CASES, "readiness", case)
    creation_ui.test_dashboard_configuration_creation_controls(tmp_path, "readiness")
