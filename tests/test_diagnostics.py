"""Diagnostic truthfulness, live probe bounds, and byte-preserved local state."""
from __future__ import annotations

import copy
import json
import socket
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_controller import config_file as config_file
from test_dashboard import EXPORTER, _publish

from ninja_pce import diagnostics, diagnostics_host
from ninja_pce.admin_model import default_setup
from ninja_pce.admin_store import AdminStore, initialize_admin
from ninja_pce.bgpls import BgplsError, _peers
from ninja_pce.cli import main
from ninja_pce.controller import load_controller_config
from ninja_pce.dashboard import DashboardStore
from ninja_pce.diagnostics_protocol import protocol_findings
from ninja_pce.state import load_state, write_state


def codes(result):
    return {item["code"] for item in (result if isinstance(result, list) else result["findings"])}


def bytes_before(root):
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_valid_captured_graph_is_evidence_not_live_rpc_or_authority(tmp_path, monkeypatch):
    config, now = _publish(tmp_path)
    config = replace(config, operating_mode="observe", required_bgpls_peers=(EXPORTER,))
    monkeypatch.setattr(diagnostics, "live_gobgp", lambda *args: pytest.fail("Implicit live RPC"))
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: pytest.fail("Diagnostic snapshot bound or connected a socket"))
    before = bytes_before(tmp_path)
    snapshot = DashboardStore(config).snapshot(now=now)
    original = copy.deepcopy(snapshot)
    result = diagnostics.snapshot_report(snapshot, include_host=False)
    assert {"BGPLS_OBSERVATION_VALID", "BGPLS_RPC_OBSERVED", "TOPOLOGY_OBSERVATION_VALID", "PCEP_DISABLED"} <= codes(result)
    assert "BGPLS_RPC_RESPONDED" not in codes(result)
    assert result["scope"] == "snapshot" and result["read_only"] is True
    assert result["control_authority_verified"] is result["forwarding_verified"] is False
    assert snapshot == original and bytes_before(tmp_path) == before


@pytest.mark.parametrize("code,expected", [("BGPLS_RPC_UNAVAILABLE", "BGPLS_RPC_UNAVAILABLE"),
                                         ("BGPLS_SESSION_DOWN", "BGPLS_SESSION_DOWN"),
                                         ("BGPLS_EOR_INCOMPLETE", "BGPLS_EOR_INCOMPLETE")])
def test_fresh_structured_collector_fault_identifies_only_its_known_cause(tmp_path, code, expected):
    config, now = _publish(tmp_path)
    write_state(config.bgpls_status_path, {"status": "unavailable", "observed_at": now.timestamp(),
                                         "diagnostic_code": code, "error": "SECRET raw collector error"})
    snapshot = DashboardStore(replace(config, operating_mode="observe")).snapshot(now=now)
    result = protocol_findings(snapshot, "observe")
    assert expected in codes(result)
    assert "SECRET" not in json.dumps(result) and "SECRET" not in json.dumps(snapshot)
    if code != "BGPLS_SESSION_DOWN":
        assert "BGPLS_SESSION_DOWN" not in codes(result)
    old = DashboardStore(replace(config, operating_mode="observe")).snapshot(now=now + timedelta(seconds=31))
    assert expected not in codes(protocol_findings(old, "observe"))


def test_stale_graph_and_optional_prefix_exclusions_do_not_claim_readiness(tmp_path):
    config, now = _publish(tmp_path)
    snapshot = DashboardStore(config).snapshot(now=now + timedelta(seconds=31))
    result = protocol_findings(snapshot, "pcep-control")
    assert {"TOPOLOGY_STALE", "BGPLS_COLLECTOR_STALE"} <= codes(result)
    assert "TOPOLOGY_OBSERVATION_VALID" not in codes(result)
    snapshot["diagnostic_observations"]["bgpls"]["excluded_prefix_count"] = 4
    snapshot["diagnostic_observations"]["bgpls"]["excluded_node_link_count"] = 2
    result = protocol_findings(snapshot, "pcep-control")
    by_code = {item["code"]: item for item in result}
    assert by_code["TOPOLOGY_PREFIX_EXCLUDED"]["severity"] == "info"
    assert by_code["TOPOLOGY_NODE_LINK_EXCLUDED"]["severity"] == "blocker"


def test_observe_never_diagnoses_retained_pcep_or_reads_its_capture(tmp_path, monkeypatch):
    config, now = _publish(tmp_path)
    config = replace(config, operating_mode="observe")
    config.pcep_path.write_text("SECRET retained PCEP evidence")
    snapshot = DashboardStore(config).snapshot(now=now)
    snapshot["peers"].append({"protocol": "PCEP", "state": "STALE", "fresh": False, "stale_reason": "deadtimer_expired"})
    result = protocol_findings(snapshot, "observe", processes=[{"component": "pcep", "state": "failed"}])
    assert [item["code"] for item in result if item["category"] == "pcep"] == ["PCEP_DISABLED"]


def test_remote_capabilities_and_deadtimer_use_peer_evidence(tmp_path):
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    peer = next(iter(capture["peers"].values()))
    peer.update(stale_reason="deadtimer_expired", fresh=False, synchronized=False, local_stateful=True)
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    result = protocol_findings(snapshot, "pcep-control")
    assert "PCEP_DEADTIMER_EXPIRED" in codes(result)
    assert "PCEP_SESSION_SYNCHRONIZED" not in codes(result)
    assert "PCEP_REMOTE_STATEFUL_OBSERVED" in codes(result)
    assert "PCEP_LOCAL_STATEFUL_ADVERTISED" in codes(result)
    peer["peer_open"] = {}
    write_state(config.pcep_path, capture)
    result = protocol_findings(DashboardStore(config).snapshot(now=now), "pcep-control")
    assert "PCEP_REMOTE_STATEFUL_UNKNOWN" in codes(result)


def test_report_limits_and_sensitive_evidence_never_become_green():
    items = [diagnostics.finding("UNKNOWN_CHECK", "unrecognized", "security", "Cause", "Review evidence",
                                password="SECRET", token="SECRET", nested={"cookie": "SECRET", "safe": "text"},
                                values=[float("nan")], long="x" * 10000)]
    result = diagnostics.report(items)
    assert result["overall"] == "unknown" and not result["complete"]
    assert "SECRET" not in json.dumps(result, allow_nan=False)
    assert len(result["findings"][0]["evidence"]["long"]) == 512
    result = diagnostics.report([diagnostics.finding(f"CHECK_{index}", "ok", "runtime", "Checked", "Review") for index in range(300)])
    assert "DIAGNOSTICS_LIMIT_REACHED" in codes(result) and not result["complete"]
    assert diagnostics.report([])["overall"] == "unknown"


def test_cli_invalid_config_is_structured_without_creating_files(tmp_path, capsys):
    before = bytes_before(tmp_path)
    assert main(["diagnostics", "--config", str(tmp_path / "missing.json")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert codes(result) == {"CONFIGURATION_INVALID"}
    assert result["read_only"] and bytes_before(tmp_path) == before


def test_cli_snapshot_uses_no_rpc_processes_or_service_commands(config_file, monkeypatch, capsys):
    path, document = config_file
    before = bytes_before(path.parent)
    monkeypatch.setattr(diagnostics_host, "_run", lambda *args, **kwargs: pytest.fail("Snapshot executed a command"))
    monkeypatch.setattr(diagnostics, "_run", lambda *args, **kwargs: pytest.fail("Snapshot executed an RPC"))
    assert main(["diagnostics", "--config", str(path), "--format", "text"]) == 1
    text = capsys.readouterr().out
    assert "CONFIGURATION_VALID" in text and "Next step:" in text
    assert bytes_before(path.parent) == before


def test_live_rpc_methods_endpoint_and_output_bounds(config_file, monkeypatch):
    from test_bgpls import peer

    config = load_controller_config(config_file[0])
    observed = []
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _value: "/reviewed/grpcurl")
    def rpc(command, **options):
        observed.append((command, options))
        value = json.dumps(peer()).encode() if command[-1].endswith("ListPeer") else b""
        return value, None
    monkeypatch.setattr(diagnostics, "_run", rpc)
    # Match the required exporter in the synthetic read-only peer response.
    document = config_file[1]
    document["bgpls"]["required_peers"] = ["172.16.100.102"]
    config_file[0].write_text(json.dumps(document))
    result = diagnostics.live_gobgp(load_controller_config(config_file[0]))
    assert "BGPLS_LIVE_PEERS_SYNCHRONIZED" in codes(result)
    assert [command[-1] for command, _ in observed] == ["apipb.GobgpApi/ListPeer", "apipb.GobgpApi/ListPath"]
    assert all(command[-2] == config.document["bgpls"]["endpoint"] for command, _ in observed)
    assert all(options["maximum"] == 2 * 1024 * 1024 and options["timeout"] <= 2 for _, options in observed)
    assert "LIVE_TOPOLOGY_QUALIFICATION_NOT_RUN" in codes(result)


@pytest.mark.parametrize("fault", ["timeout", "output_limit", "command_failed"])
def test_live_rpc_failures_preserve_known_cause_without_output(config_file, monkeypatch, fault):
    monkeypatch.setattr(diagnostics.shutil, "which", lambda _value: "/reviewed/grpcurl")
    monkeypatch.setattr(diagnostics, "_run", lambda *args, **kwargs: (b"SECRET native output", fault))
    result = diagnostics.live_gobgp(load_controller_config(config_file[0]))
    assert "SECRET" not in json.dumps(result)
    assert result[0]["evidence"]["result"] == fault
    assert "BGPLS_RPC_RESPONDED" not in codes(result)


def test_diagnostics_service_bounds_concurrent_live_requests(monkeypatch):
    service = diagnostics.DiagnosticService()
    calls = []
    monkeypatch.setattr(diagnostics, "admin_report", lambda *args, **kwargs: calls.append(kwargs) or diagnostics.report([]))
    service.lock.acquire()
    try:
        assert codes(service.run(None, live=True)) == {"DIAGNOSTICS_BUSY"}
        assert calls == []
    finally:
        service.lock.release()
    service.run(None, live=True)
    assert codes(service.run(None, live=True)) == {"DIAGNOSTICS_RATE_LIMITED"}
    assert len(calls) == 1


def test_directory_diagnostics_do_not_take_owner_lock_or_change_private_state(tmp_path):
    root, _ = initialize_admin(tmp_path / "observer")
    store = AdminStore(root)
    try:
        preview = store.preview(default_setup(), None, "operator")
        store.apply(preview["preview_id"], None, "operator")
        marker = root / "data/state/retained-ownership.json"
        marker.write_bytes(b"original ownership and reservation history")
        before = bytes_before(root)
        result = diagnostics.directory_report(root)
        assert "CONFIGURATION_VALID" in codes(result)
        assert "ADMIN_WORKSPACE_INVALID" not in codes(result)
        assert bytes_before(root) == before
        result = diagnostics.admin_report(store, authenticated=True)
        assert "AUTHENTICATED_ADMIN_REQUEST" in codes(result)
        assert bytes_before(root) == before
    finally:
        store.close()


def test_directory_inventory_tampering_is_blocked_without_probing(tmp_path, monkeypatch):
    root, _ = initialize_admin(tmp_path / "observer")
    store = AdminStore(root)
    preview = store.preview(default_setup(), None, "operator")
    store.apply(preview["preview_id"], None, "operator")
    store.close()
    (root / "revisions" / preview["revision"] / "gobgpd.toml").write_text("SECRET unreviewed config")
    monkeypatch.setattr(diagnostics, "live_gobgp", lambda *args: pytest.fail("Probed tampered revision"))
    result = diagnostics.directory_report(root, live=True)
    assert codes(result) == {"ADMIN_WORKSPACE_INVALID"}
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("field,value,expected", [("sessionState", "IDLE", "BGPLS_SESSION_DOWN"),
                                                 ("sessionState", None, "BGPLS_PEER_METADATA_INVALID")])
def test_peer_errors_have_machine_codes_without_inventing_down_state(field, value, expected):
    from test_bgpls import PEER, peer

    value_peer = peer()
    value_peer["peer"]["state"][field] = value
    with pytest.raises(BgplsError) as caught:
        _peers([value_peer], (PEER,))
    assert caught.value.code == expected


def test_capped_owned_command_does_not_hang_or_read_secrets(tmp_path):
    output, error = diagnostics_host._run([sys.executable, "-c", "print('x'*1000000)"], timeout=.5, maximum=1024)
    assert error == "output_limit" and output == b""
    output, error = diagnostics_host._run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=.05)
    assert error == "timeout" and output == b""


def test_host_checks_and_artifact_metadata_are_read_only(tmp_path, monkeypatch):
    marker = tmp_path / "existing.json"
    marker.write_text("SECRET existing journal")
    marker.chmod(0o600)
    before = bytes_before(tmp_path)
    monkeypatch.setattr(diagnostics_host, "_run", lambda *args, **kwargs: pytest.fail("Static host check ran a command"))
    result = diagnostics_host.host_findings(paths=[marker, tmp_path / "absent"], addresses=["127.0.0.1"], live=False)
    assert "HOST_PATH_MISSING" in codes(result)
    assert "SECRET" not in json.dumps(result)
    assert bytes_before(tmp_path) == before
    result = diagnostics_host.artifact_findings(grpcurl=sys.executable)
    assert "GRPCURL_BINARY_AVAILABLE" in codes(result)
    assert "GRPCURL_PROVENANCE_UNKNOWN" in codes(result)


@pytest.mark.parametrize("active,result,pid,expected", [("active", "success", "123", "SERVICE_RUNNING"),
    ("active", "success", "0", "SERVICE_STATE_UNKNOWN"), ("failed", "start-limit-hit", "0", "SERVICE_FAILED"),
    ("inactive", "success", "0", "SERVICE_NOT_RUNNING")])
def test_service_diagnostics_use_only_fixed_properties_and_commands(monkeypatch, active, result, pid, expected):
    calls = []
    monkeypatch.setattr(diagnostics_host, "_executable", lambda _name: Path("/usr/bin/systemctl"))
    def run(command, **kwargs):
        calls.append(command)
        return f"LoadState=loaded\nActiveState={active}\nSubState=running\nResult={result}\nMainPID={pid}\nNRestarts=7\nUnitFileState=enabled\n".encode(), None
    monkeypatch.setattr(diagnostics_host, "_run", run)
    result_findings = diagnostics_host.service_findings(("ninja-admin.service",), live=True)
    assert expected in codes(result_findings)
    assert len(calls) == 1 and calls[0][1:3] == ["--no-pager", "show"]
    assert calls[0][-2:] == ["--", "ninja-admin.service"]
    calls.clear()
    assert "SERVICE_UNIT_NOT_ALLOWED" in codes(diagnostics_host.service_findings(("other.service;kill",), live=True))
    assert calls == []


def test_probe_exception_reports_incomplete_scope_without_blaming_valid_configuration(config_file, monkeypatch):
    def probe(*args, **kwargs):
        raise OSError("SECRET inaccessible metadata")
    monkeypatch.setattr(diagnostics, "snapshot_report", probe)
    result = diagnostics.config_report(config_file[0], live=True)
    assert codes(result) == {"DIAGNOSTICS_CHECK_INCOMPLETE"}
    assert result["scope"] == "live" and result["overall"] == "unknown"
    assert "SECRET" not in json.dumps(result)


def test_dangling_active_pointer_is_invalid_and_never_probed(tmp_path, monkeypatch):
    root, _ = initialize_admin(tmp_path / "observer")
    (root / "admin/active.json").symlink_to(root / "missing")
    monkeypatch.setattr(diagnostics, "live_gobgp", lambda *args: pytest.fail("Probed unsafe workspace"))
    assert codes(diagnostics.directory_report(root, live=True)) == {"ADMIN_WORKSPACE_INVALID"}


@pytest.mark.parametrize("eor", [None, "true", 1])
def test_missing_or_malformed_eor_is_not_reported_as_an_explicit_negative(eor):
    from test_bgpls import PEER, peer

    value = peer()
    value["peer"]["afiSafis"][0]["mpGracefulRestart"]["state"]["endOfRibReceived"] = eor
    with pytest.raises(BgplsError) as caught:
        _peers([value], (PEER,))
    assert caught.value.code == "BGPLS_PEER_METADATA_INVALID"


@pytest.mark.parametrize("malformed", [{"state": []}, {"mpGracefulRestart": None}, {"timers": {"state": None}}])
def test_malformed_live_peer_response_is_structured_and_does_not_escape(config_file, monkeypatch, malformed):
    from test_bgpls import peer

    config = load_controller_config(config_file[0])
    value = peer()
    value["peer"]["state"]["neighborAddress"] = config.document["bgpls"]["required_peers"][0]
    if "timers" in malformed:
        value["peer"].update(malformed)
    else:
        value["peer"]["afiSafis"][0].update(malformed)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/trusted/grpcurl")
    monkeypatch.setattr(diagnostics, "_run", lambda *args, **kwargs: (json.dumps(value).encode(), None))
    result = diagnostics.live_gobgp(config)
    assert "BGPLS_LIVE_PEER_METADATA_INVALID" in codes(result)
    assert "BGPLS_LIVE_PEERS_SYNCHRONIZED" not in codes(result)
