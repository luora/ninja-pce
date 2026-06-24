from pathlib import Path

import yaml

from ninja_pce.reconcile import reconcile_once

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples" / "topologies" / "simple_multiarea.yml"
DEMAND = ROOT / "examples" / "demands" / "pe1_to_pe3_50m.yml"
INVENTORY = ROOT / "gns3" / "lab-data" / "inventory" / "gns3_inventory.yml"


class FakeConnection:
    def __init__(self):
        self.commands = []

    def send_command(self, command):
        return f"ok: {command}"

    def send_config_set(self, commands, **kwargs):
        self.commands = list(commands)
        return "accepted"

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
    assert unchanged.action == "unchanged"
