from pathlib import Path

import pytest
import yaml

from ninja_pce.reconcile import reconcile_once

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"
DEMAND = ROOT / "examples" / "demands" / "pe1_to_pe3_50m.yml"
INVENTORY = ROOT / "gns3" / "lab-data" / "inventory" / "gns3_inventory.yml"


class FakeConnection:
    def __init__(self):
        self.commands = []
        self.writes = 0
        self.reads = []

    def send_command(self, command, **kwargs):
        self.reads.append(command)
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
        self.commands = list(commands)
        self.writes += 1
        return "accepted"

    def commit(self, **kwargs):
        return "commit complete"

    def exit_config_mode(self):
        pass

    def disconnect(self):
        pass


def test_reconcile_previews_then_suppresses_unchanged_inputs(tmp_path):
    state = tmp_path / "state.json"
    output = tmp_path / "outputs"

    first = reconcile_once(
        topology_path=TOPOLOGY,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=output,
        state_path=state,
    )
    second = reconcile_once(
        topology_path=TOPOLOGY,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=output,
        state_path=state,
    )

    assert first.action == "preview"
    assert first.topology_changed
    assert first.diff
    assert second.action == "unchanged"
    assert not second.diff
    assert state.exists()
    assert state.with_suffix(".audit.jsonl").exists()


def test_semantically_identical_topology_reordering_is_a_noop(tmp_path):
    state = tmp_path / "state.json"
    copied_topology = tmp_path / "topology.yml"
    data = yaml.safe_load(TOPOLOGY.read_text(encoding="utf-8"))
    copied_topology.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    reconcile_once(
        topology_path=copied_topology,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=tmp_path / "outputs",
        state_path=state,
    )
    data["nodes"].reverse()
    data["links"].reverse()
    copied_topology.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    outcome = reconcile_once(
        topology_path=copied_topology,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=tmp_path / "outputs",
        state_path=state,
    )

    assert outcome.action == "unchanged"
    assert not outcome.topology_changed


def test_topology_metric_change_triggers_new_desired_config(tmp_path):
    state = tmp_path / "state.json"
    copied_topology = tmp_path / "topology.yml"
    data = yaml.safe_load(TOPOLOGY.read_text(encoding="utf-8"))
    copied_topology.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    reconcile_once(
        topology_path=copied_topology,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=tmp_path / "outputs",
        state_path=state,
    )
    for link in data["links"]:
        if link["id"] == "PE1-P3":
            link["metric"] = 100
    copied_topology.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    outcome = reconcile_once(
        topology_path=copied_topology,
        demand_path=DEMAND,
        inventory_path=INVENTORY,
        output_dir=tmp_path / "outputs",
        state_path=state,
    )

    assert outcome.action == "preview"
    assert outcome.topology_changed
    assert outcome.desired_config_changed
    assert outcome.result.path_nodes != ("PE1", "P3", "P4", "PE3")


def test_preview_can_be_explicitly_applied_without_another_topology_change(tmp_path):
    state = tmp_path / "state.json"
    connection = FakeConnection()
    common = {
        "topology_path": TOPOLOGY,
        "demand_path": DEMAND,
        "inventory_path": INVENTORY,
        "output_dir": tmp_path / "outputs",
        "state_path": state,
    }

    reconcile_once(**common)
    outcome = reconcile_once(
        **common,
        execute=True,
        approved=True,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **kwargs: connection,
    )

    assert outcome.action == "applied"
    assert connection.commands == list(outcome.plan.commands)

    unchanged = reconcile_once(
        **common,
        execute=True,
        approved=True,
        environ={"NINJA_PCE_PE1_PASSWORD": "test-secret"},
        connect_handler=lambda **kwargs: connection,
    )
    assert unchanged.action == "revalidated"
    assert connection.writes == 1


def test_execute_with_changed_demand_rejects_stale_preview_before_network(tmp_path):
    import pytest
    demand = tmp_path / "demand.yml"
    data = yaml.safe_load(DEMAND.read_text())
    demand.write_text(yaml.safe_dump(data))
    common = dict(topology_path=TOPOLOGY, demand_path=demand, inventory_path=INVENTORY,
                  output_dir=tmp_path / "output", state_path=tmp_path / "state.json")
    preview = reconcile_once(**common)
    data["service"]["bandwidth_mbps"] = 51
    demand.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="stale"):
        reconcile_once(**common, execute=True, approved=True, approved_plan_id=preview.plan_id,
                       connect_handler=lambda **kwargs: pytest.fail("Network must not be contacted"))


def test_partial_write_restart_is_read_only_and_retains_resources(tmp_path):
    from ninja_pce.reservations import ReservationLedger
    from ninja_pce.state import load_state
    common = dict(topology_path=TOPOLOGY, demand_path=DEMAND, inventory_path=INVENTORY,
                  output_dir=tmp_path / "output", state_path=tmp_path / "state.json")
    preview = reconcile_once(**common)
    connection = FakeConnection()

    def lose_connection(commands, **kwargs):
        raise RuntimeError("SSH lost after uncertain write")
    connection.send_config_set = lose_connection
    uncertain = reconcile_once(**common, execute=True, approved=True, approved_plan_id=preview.plan_id,
                               environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                               connect_handler=lambda **kwargs: connection)
    assert uncertain.action == "verification_failed"
    assert load_state(tmp_path / "state.json")["pending"]
    assert ReservationLedger(tmp_path).entries["PE1:iosxr:tunnel-te100"]["resources"]
    recovered = FakeConnection()
    result = reconcile_once(**common, execute=True, approved=True,
                            environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                            connect_handler=lambda **kwargs: recovered)
    assert result.action == "recovery_required"
    assert result.push_result.status == "APPLIED_UNVERIFIED"
    assert not result.push_result.verified
    assert "confirmed commit may still expire" in result.push_result.recovery_note
    assert recovered.writes == 0
    recovered_state = load_state(tmp_path / "state.json")
    assert recovered_state["pending"]
    assert "last_verified" not in recovered_state
    assert recovered_state["status"] == "APPLIED_UNVERIFIED"
    assert ReservationLedger(tmp_path).entries["PE1:iosxr:tunnel-te100"]["status"] == "APPLIED_UNVERIFIED"


def test_kill_switch_blocks_execute_before_network(tmp_path):
    import pytest
    common = dict(topology_path=TOPOLOGY, demand_path=DEMAND, inventory_path=INVENTORY,
                  output_dir=tmp_path / "output", state_path=tmp_path / "state.json")
    preview = reconcile_once(**common)
    with pytest.raises(ValueError, match="PREVIEW_ONLY"):
        reconcile_once(**common, execute=True, approved=True, approved_plan_id=preview.plan_id,
                       environ={"NINJA_PCE_PREVIEW_ONLY": "true"},
                       connect_handler=lambda **kwargs: pytest.fail("No network calls permitted"))


def test_network_failure_cannot_erase_planned_reservation(tmp_path):
    import pytest

    from ninja_pce.reservations import ReservationLedger
    common = dict(topology_path=TOPOLOGY, demand_path=DEMAND, inventory_path=INVENTORY,
                  output_dir=tmp_path / "output", state_path=tmp_path / "state.json")
    preview = reconcile_once(**common)
    with pytest.raises(Exception, match="is not set"):
        reconcile_once(**common, execute=True, approved=True, approved_plan_id=preview.plan_id, environ={})
    assert ReservationLedger(tmp_path).entries["PE1:iosxr:tunnel-te100"]["status"] == "ROLLBACK_REQUIRED"


def test_expired_approval_rejects_even_read_only_revalidation(tmp_path):
    from datetime import datetime, timedelta, timezone

    import pytest
    with pytest.raises(ValueError, match="expired"):
        reconcile_once(topology_path=TOPOLOGY, demand_path=DEMAND, inventory_path=INVENTORY,
                       state_path=tmp_path / "state.json", output_dir=tmp_path / "output",
                       execute=True, approved=True,
                       approval_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                       connect_handler=lambda **kwargs: pytest.fail("Expired approval must fail before contact"))


def test_input_change_during_network_prechecks_blocks_actual_write(tmp_path):
    import pytest
    topology = tmp_path / "ted.yml"
    topology.write_text(TOPOLOGY.read_text())
    common = dict(topology_path=topology, demand_path=DEMAND, inventory_path=INVENTORY,
                  state_path=tmp_path / "state.json", output_dir=tmp_path / "output")
    preview = reconcile_once(**common)
    connection = FakeConnection()
    original = connection.send_command

    def churn(command, **kwargs):
        if command == "show version":
            data = yaml.safe_load(topology.read_text())
            data["links"][0]["metric"] += 1
            topology.write_text(yaml.safe_dump(data))
        return original(command, **kwargs)

    connection.send_command = churn
    with pytest.raises(Exception, match="Inputs changed before application"):
        reconcile_once(**common, execute=True, approved=True, approved_plan_id=preview.plan_id,
                       environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                       connect_handler=lambda **kwargs: connection)
    assert connection.writes == 0


@pytest.mark.parametrize("capacity,admitted", [(0.000999, False), (0.001, True)])
def test_admission_covers_actual_router_signaling_quantum(tmp_path, capacity, admitted):
    from ninja_pce.cspf import CspfNoPath
    from ninja_pce.reservations import ReservationLedger
    topology = {"metadata": {"name": "quantum"},
                "nodes": [{"id": "A", "loopback": "10.255.0.1"}, {"id": "B", "loopback": "10.255.0.2"}],
                "links": [{"id": "A-B", "from": "A", "to": "B", "metric": 1,
                           "reservable_bw_mbps": capacity, "from_ip": "10.0.0.1", "to_ip": "10.0.0.2", "bidirectional": False}]}
    demand = {"service": {"name": "quantum", "source": "A", "destination": "B", "bandwidth_mbps": 0.000001}}
    inventory = {"devices": {node: {"platform": "cisco_xr", "management_ip": f"192.0.2.{index}",
                                   "username": "synthetic"} for index, node in enumerate(("A", "B"), 1)}}
    for name, data in (("topology", topology), ("demand", demand), ("inventory", inventory)):
        (tmp_path / f"{name}.yml").write_text(yaml.safe_dump(data))
    common = dict(topology_path=tmp_path / "topology.yml", demand_path=tmp_path / "demand.yml",
                  inventory_path=tmp_path / "inventory.yml", state_path=tmp_path / "state" / "lsp.json",
                  output_dir=tmp_path / "output")
    if admitted:
        result = reconcile_once(**common)
        assert result.result.demand.bandwidth_bps == 1000
        assert ReservationLedger(tmp_path / "state").entries["A:iosxr:tunnel-te100"]["current_bandwidth_bps"] == 1000
    else:
        with pytest.raises(CspfNoPath):
            reconcile_once(**common)
