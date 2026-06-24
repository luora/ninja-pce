from pathlib import Path

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

    def send_command(self, command):
        self.show_commands.append(command)
        return f"ok: {command}"

    def send_config_set(self, commands, **kwargs):
        self.config_commands = list(commands)
        return "accepted"

    def disconnect(self):
        pass


def _write_config(tmp_path, text):
    config = tmp_path / "daemon.yml"
    config.write_text(text.strip(), encoding="utf-8")
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
    assert [result.action for result in second.demand_results] == ["unchanged", "unchanged"]
    assert all(not result.policy_reasons for result in second.demand_results)


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
