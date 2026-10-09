from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from ninja_pce.daemon import load_daemon_config, run_daemon_cycle

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"
DEMAND = ROOT / "examples" / "demands" / "pe1_to_pe3_50m.yml"
DEMAND_120M = ROOT / "examples" / "demands" / "pe1_to_pe3_120m.yml"
INVENTORY = ROOT / "gns3" / "lab-data" / "inventory" / "gns3_inventory.yml"


class FakeConnection:
    def __init__(self):
        self.config_commands = []
        self.show_commands = []

    def send_command(self, command, **kwargs):
        self.show_commands.append(command)
        if command == "show version":
            return "Cisco IOS XR Software, Version 7.9.2"
        if command.startswith("show route "):
            return "Routing entry for 10.255.0.6/32"
        if "traffic-eng tunnels" in command:
            return """Name: tunnel-te100 Destination: 10.255.0.6
Admin: up Oper: up Path: valid Signalling: connected
Bandwidth: 50000 kbps
Current LSP Info:
Explicit Route:
 Strict, 10.0.13.3
 Strict, 10.0.34.4
 Strict, 10.0.46.6
Tspec: avg rate=50000 kbits
Resv Info:
 Fspec: avg rate=50000 kbits
"""
        return ""

    def send_config_set(self, commands, **kwargs):
        self.config_commands = list(commands)
        return "accepted"

    def disconnect(self):
        pass

    def commit(self, **kwargs):
        return "commit complete"

    def exit_config_mode(self):
        pass


def _write_config(tmp_path, text):
    topology = yaml.safe_load(TOPOLOGY.read_text())
    topology["metadata"].update({"source": "synthetic-tests", "timestamp": datetime.now(timezone.utc).isoformat(),
                                 "authoritative": True, "complete": True})
    fresh = tmp_path / "fresh-topology.yml"
    fresh.write_text(yaml.safe_dump(topology))
    config = tmp_path / "daemon.yml"
    config.write_text(text.strip().replace(str(TOPOLOGY), str(fresh)) + "\ndebounce_seconds: 0\n", encoding="utf-8")
    return load_daemon_config(config)


def test_manual_daemon_cycle_previews_then_suppresses_unchanged_demands(tmp_path):
    demand_dir = tmp_path / "demands"
    demand_dir.mkdir()
    (demand_dir / DEMAND.name).write_text(DEMAND.read_text(encoding="utf-8"), encoding="utf-8")
    (demand_dir / DEMAND_120M.name).write_text(DEMAND_120M.read_text(encoding="utf-8"), encoding="utf-8")

    config = _write_config(
        tmp_path,
        f"""
topology_feed:
  type: file
  path: {TOPOLOGY}
demands:
  type: directory
  path: {demand_dir}
inventory: {INVENTORY}
output_dir: {tmp_path / "outputs"}
state_dir: {tmp_path / "state"}
approval_policy:
  mode: manual
health:
  enabled: false
""",
    )

    first = run_daemon_cycle(config)
    second = run_daemon_cycle(config)

    assert first.status == "ok"
    assert [result.action for result in first.demand_results] == ["preview", "preview"]
    assert all(not result.auto_approved for result in first.demand_results)
    assert all(result.policy_reasons == ("approval_policy.mode is manual",) for result in first.demand_results)
    # B's reservation changes A's admission evidence once, even if commands remain identical.
    assert [result.action for result in second.demand_results] == ["preview", "unchanged"]
    third = run_daemon_cycle(config)
    assert all(result.action == "unchanged" for result in third.demand_results)


def test_guarded_auto_applies_when_preview_passes_policy(tmp_path):
    connection = FakeConnection()
    config = _write_config(
        tmp_path,
        f"""
topology_feed:
  type: file
  path: {TOPOLOGY}
demands:
  type: file
  path: {DEMAND}
inventory: {INVENTORY}
output_dir: {tmp_path / "outputs"}
state_dir: {tmp_path / "state"}
approval_policy:
  mode: guarded-auto
  max_commands: 50
  min_residual_bw_mbps: 10
  allowed_demand_names: [CUSTOMER_A_PE1_PE3_50M]
health:
  enabled: false
""",
    )

    cycle = run_daemon_cycle(
        config,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **kwargs: connection,
    )

    assert cycle.status == "ok"
    assert len(cycle.demand_results) == 1
    result = cycle.demand_results[0]
    assert result.action == "applied"
    assert result.auto_approved
    assert not result.policy_reasons
    assert connection.config_commands
    assert connection.show_commands


def test_guarded_auto_downgrades_to_preview_when_secret_is_missing(tmp_path):
    config = _write_config(
        tmp_path,
        f"""
topology_feed:
  type: file
  path: {TOPOLOGY}
demands:
  type: file
  path: {DEMAND}
inventory: {INVENTORY}
output_dir: {tmp_path / "outputs"}
state_dir: {tmp_path / "state"}
approval_policy:
  mode: guarded-auto
health:
  enabled: false
""",
    )

    cycle = run_daemon_cycle(config, environ={})

    assert cycle.status == "ok"
    result = cycle.demand_results[0]
    assert result.action == "preview"
    assert not result.auto_approved
    assert "required secret NINJA_PCE_PE1_PASSWORD is not set" in result.policy_reasons


def _single_config(tmp_path, mode="guarded-auto"):
    return _write_config(tmp_path, f"""
topology_feed:
  path: {TOPOLOGY}
demands:
  type: file
  path: {DEMAND}
inventory: {INVENTORY}
state_dir: {tmp_path / 'state'}
output_dir: {tmp_path / 'outputs'}
approval_policy:
  mode: {mode}
health:
  enabled: false
""")


@pytest.mark.parametrize("field,value", [
    ("poll_interval", "nan"), ("poll_interval", "-1"), ("poll_interval", "true"),
    ("approval_policy", {"allow_warnings": "false"}),
    ("health", {"enabled": "false"}), ("health", {"port": 65536}),
    ("retry_attempts", 0), ("debounce_seconds", float("inf")),
    ("topology_feed", {"path": str(TOPOLOGY), "max_age_seconds": -1}),
    ("topology_feed", []), ("approval_policy", {"max_commands": 1.2}),
    ("approval_policy", {"allowed_demand_names": [17]}), ("unknown_option", True),
])
def test_daemon_config_rejects_unsafe_coercion_and_malformed_fields(tmp_path, field, value):
    from ninja_pce.loaders import InputError
    data = {"topology_feed": {"path": str(TOPOLOGY)}, "demands": {"path": str(DEMAND)}, "inventory": str(INVENTORY)}
    data[field] = value
    path = tmp_path / "bad.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(InputError):
        load_daemon_config(path)


def test_daemon_config_rejects_duplicate_mapping_keys(tmp_path):
    from ninja_pce.loaders import InputError
    path = tmp_path / "duplicate.yml"
    path.write_text("approval_policy: {mode: manual, mode: guarded-auto}\n")
    with pytest.raises(InputError, match="Duplicate"):
        load_daemon_config(path)


def test_guarded_auto_rejects_non_authoritative_ted_with_zero_network_calls(tmp_path):
    config = _single_config(tmp_path)
    data = yaml.safe_load(config.topology_feed.path.read_text())
    data["metadata"].pop("authoritative")
    config.topology_feed.path.write_text(yaml.safe_dump(data))
    cycle = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                             connect_handler=lambda **kwargs: pytest.fail("Unauthorized network access"))
    assert cycle.status == "degraded"
    assert cycle.demand_results[0].action == "preview"
    assert any("authoritative" in reason for reason in cycle.demand_results[0].policy_reasons)


def test_stale_metadata_blocks_even_with_fresh_file_mtime(tmp_path):
    from datetime import timedelta
    config = _single_config(tmp_path)
    data = yaml.safe_load(config.topology_feed.path.read_text())
    data["metadata"]["timestamp"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    config.topology_feed.path.write_text(yaml.safe_dump(data))
    cycle = run_daemon_cycle(config, connect_handler=lambda **kwargs: pytest.fail("Stale topology access"))
    assert cycle.status == "stale_topology"
    assert not cycle.demand_results


def test_invalid_and_missing_topology_produce_unhealthy_cycle(tmp_path):
    config = _single_config(tmp_path)
    config.topology_feed.path.unlink()
    cycle = run_daemon_cycle(config)
    assert cycle.status == "invalid_topology"
    assert not cycle.topology_fresh


def test_debounce_waits_for_stable_topology_and_coalesces_timestamp_updates(tmp_path):
    from dataclasses import replace
    config = replace(_single_config(tmp_path), debounce_seconds=10)
    first = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                             connect_handler=lambda **kwargs: pytest.fail("Debounce must prevent network calls"))
    assert first.demand_results[0].action == "debounced"
    from ninja_pce.state import load_state
    runtime = config.state_dir / ".ninja-pce-daemon-runtime.json"
    first_seen = next(iter(load_state(runtime)["demands"].values()))["first_seen"]
    data = yaml.safe_load(config.topology_feed.path.read_text())
    data["metadata"]["timestamp"] = datetime.now(timezone.utc).isoformat()
    config.topology_feed.path.write_text(yaml.safe_dump(data))
    second = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"})
    assert second.demand_results[0].action == "debounced"
    assert next(iter(load_state(runtime)["demands"].values()))["first_seen"] == first_seen


def test_unverified_push_has_bounded_retry_and_survives_daemon_restart(tmp_path):
    config = _single_config(tmp_path)
    connection = FakeConnection()
    original = connection.send_command
    connection.send_command = lambda command, **kwargs: "ambiguous output" if "traffic-eng tunnels" in command else original(command, **kwargs)
    first = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                            connect_handler=lambda **kwargs: connection)
    assert first.status == "degraded"
    assert first.demand_results[0].action == "verification_failed"
    second = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                             connect_handler=lambda **kwargs: pytest.fail("Backoff must persist"))
    assert second.demand_results[0].action == "backoff"
    assert second.status == "degraded"


def test_kill_switch_preserves_preview_and_makes_no_network_calls(tmp_path):
    config = _single_config(tmp_path)
    cycle = run_daemon_cycle(config, environ={"NINJA_PCE_PE1_PASSWORD": "synthetic", "NINJA_PCE_PREVIEW_ONLY": "yes"},
                             connect_handler=lambda **kwargs: pytest.fail("Kill switch must block access"))
    assert cycle.demand_results[0].action == "preview"
    assert "NINJA_PCE_PREVIEW_ONLY is set" in cycle.demand_results[0].policy_reasons


def test_command_allowlist_rejects_suffix_injection(tmp_path):
    from ninja_pce.daemon import _command_allowed
    policy = _single_config(tmp_path).approval_policy
    assert _command_allowed("destination 10.255.0.6", "iosxr", policy)
    assert not _command_allowed("destination 10.255.0.6 ; reload", "iosxr", policy)
    assert not _command_allowed("interface tunnel-te100 shutdown", "iosxr", policy)
    assert not _command_allowed("set protocols mpls path x 10.0.0.1 strict delete", "junos", policy)


def test_health_reports_uninitialized_and_failed_cycles_as_unhealthy(tmp_path, monkeypatch):
    from io import BytesIO

    import ninja_pce.daemon as daemon
    from ninja_pce.daemon import HealthConfig, HealthState, start_health_server
    captured = {}

    def capture_server(address, handler):
        captured["handler"] = handler
        return object()

    monkeypatch.setattr(daemon, "ThreadingHTTPServer", capture_server)
    state = HealthState()
    start_health_server(HealthConfig(), state)

    def request():
        handler = object.__new__(captured["handler"])
        handler.path = "/healthz"
        handler.wfile = BytesIO()
        statuses = []
        handler.send_response = lambda status: statuses.append(status)
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        return statuses[0]

    assert request() == 503
    config = _single_config(tmp_path, mode="manual")
    state.record(run_daemon_cycle(config))
    assert request() == 200
    config.topology_feed.path.unlink()
    state.record(run_daemon_cycle(config))
    assert request() == 503
