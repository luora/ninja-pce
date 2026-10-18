"""Application assembly, read-only validation and process lifecycle contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from test_pcep_compute_cli import offline_server as offline_server

from ninja_pce import controller
from ninja_pce.cli import build_parser, main
from ninja_pce.state import load_state, write_state

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_file(tmp_path):
    policy = json.loads((ROOT / "examples/pcep/topology-domain-policy.json").read_text())
    (tmp_path / "domain.json").write_text(json.dumps(policy))
    document = {
        "version": 1,
        "files": {"bgpls_raw": "live/raw.json", "bgpls_status": "live/status.json",
                  "topology": "live/topology.json", "pcep_capture": "live/pcep.json"},
        "state_dir": "state",
        "bgpls": {"endpoint": "127.0.0.1:50051", "required_peers": ["192.0.2.1"]},
        "pcep": {"listen_host": "192.0.2.10", "domain_policy": "domain.json"},
        "dashboard": {},
    }
    path = tmp_path / "controller.json"
    path.write_text(json.dumps(document))
    return path, document


def save_config(config_file, document=None):
    path, original = config_file
    path.write_text(json.dumps(document if document is not None else original))
    return path


def test_validate_is_read_only_and_does_not_require_live_state(config_file, capsys):
    path, _ = config_file
    before = {item.relative_to(path.parent): item.read_bytes() for item in path.parent.rglob("*") if item.is_file()}
    assert main(["controller", "validate", "--config", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["live_health_checked"] is False
    after = {item.relative_to(path.parent): item.read_bytes() for item in path.parent.rglob("*") if item.is_file()}
    assert before == after
    assert not (path.parent / "state").exists()
    assert not (path.parent / "live").exists()


def test_defaults_and_relative_paths_are_canonical_and_immutable(config_file, monkeypatch):
    path, _ = config_file
    monkeypatch.chdir("/")
    config = controller.load_controller_config(path)
    data = config.document
    assert data["files"]["pcep_capture"] == str(path.parent / "live/pcep.json")
    assert data["runtime_dir"] == str(path.parent / "live/.controller")
    assert data["pcep"]["port"] == 4189
    assert data["pcep"]["allowed_peers"] == []
    assert data["pcep"]["headends"] == {}
    assert data["pcep"]["delegated_tunnels"] == {}
    assert data["dashboard"] == {"enabled": True, "listen_host": "127.0.0.1", "port": 8088,
                                  "refresh_seconds": 5.0}
    assert Path(data["bgpls"]["proto_path"]).is_file()
    data["pcep"]["port"] = 1
    assert config.document["pcep"]["port"] == 4189


@pytest.mark.parametrize(("section", "field", "value"), [
    (None, "version", True), (None, "version", 2), (None, "extra", 1),
    ("files", "extra", "other.json"), ("pcep", "port", True), ("pcep", "port", 0),
    ("pcep", "port", 65536), ("pcep", "listen_host", "router.example"),
    ("pcep", "allowed_peers", ["192.0.2.1", "192.0.2.1"]),
    ("pcep", "allowed_peers", ["0.0.0.0"]), ("pcep", "headends", ["192.0.2.1"]),
    ("pcep", "keepalive_seconds", 30),  # Dead timer equal to keepalive, set below.
    ("bgpls", "endpoint", "192.0.2.1:50051"), ("bgpls", "endpoint", "127.0.0.1:0"),
    ("bgpls", "required_peers", []), ("bgpls", "timeout_seconds", 31),
    ("bgpls", "max_age_seconds", 31), ("bgpls", "interval_seconds", 0),
    ("bgpls", "grpcurl", "--bad-option"), ("bgpls", "node_map", "missing.json"),
    ("pcep", "domain_policy", "missing.json"), ("pcep", "provisioning_state", "missing.json"),
    ("dashboard", "enabled", "true"), ("dashboard", "listen_host", "0.0.0.0"),
    ("dashboard", "port", False), ("dashboard", "port", -1),
    (None, "runtime_dir", "state/runtime"), (None, "state_dir", "domain.json"),
    (None, "runtime_dir", "."), ("files", "topology", "state/topology.json"),
    ("pcep", "domain_policy", "state/domain.json"),
])
def test_rejects_bad_schema_types_ranges_and_input_refs(config_file, section, field, value):
    _, document = config_file
    if section == "pcep" and field == "keepalive_seconds":
        document["pcep"]["deadtimer_seconds"] = 30
    target = document if section is None else document[section]
    target[field] = value
    with pytest.raises(ValueError):
        controller.load_controller_config(save_config(config_file))


def test_missing_required_fields_are_rejected(config_file):
    _, document = config_file
    del document["pcep"]["domain_policy"]
    with pytest.raises(ValueError, match="missing"):
        controller.load_controller_config(save_config(config_file))


def test_duplicate_keys_and_nonfinite_values_are_rejected(config_file):
    path, document = config_file
    original = json.dumps(document)
    for broken in (original.replace('"version": 1', '"version": 1, "version": 1'),
                   original.replace('"dashboard": {}', '"dashboard": {"port": NaN}')):
        path.write_text(broken)
        with pytest.raises(ValueError):
            controller.load_controller_config(path)


def test_configuration_and_static_references_reject_direct_symlinks(config_file):
    path, document = config_file
    alias = path.parent / "configuration-alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="symbolic link"):
        controller.load_controller_config(alias)
    (path.parent / "policy-alias.json").symlink_to(path.parent / "domain.json")
    document["pcep"]["domain_policy"] = "policy-alias.json"
    with pytest.raises(ValueError, match="symbolic link"):
        controller.load_controller_config(save_config(config_file))


def test_static_input_inside_writable_state_is_rejected_even_when_present(config_file):
    path, document = config_file
    (path.parent / "state").mkdir()
    (path.parent / "state/domain.json").write_bytes((path.parent / "domain.json").read_bytes())
    document["pcep"]["domain_policy"] = "state/domain.json"
    with pytest.raises(ValueError, match="Static controller inputs"):
        controller.load_controller_config(save_config(config_file))


@pytest.mark.parametrize("field,target", [
    ("topology", "live/raw.json"), ("pcep_capture", "domain.json"),
    ("bgpls_status", "controller.json"),
    ("pcep_capture", "live/.controller/.ninja-pce-controller-pcep.json"),
])
def test_output_aliases_cannot_overwrite_config_policy_or_runtime(config_file, field, target):
    _, document = config_file
    document["runtime_dir"] = "live/.controller"
    document["files"][field] = target
    with pytest.raises(ValueError):
        controller.load_controller_config(save_config(config_file))


def test_symlink_aliases_are_detected_without_writing(config_file):
    path, document = config_file
    (path.parent / "alias.json").symlink_to(path.parent / "domain.json")
    document["files"]["topology"] = "alias.json"
    with pytest.raises(ValueError, match="symbolic link"):
        controller.load_controller_config(save_config(config_file))


def test_overlapping_socket_binds_are_rejected(config_file):
    _, document = config_file
    document["pcep"].update(listen_host="0.0.0.0", port=8088)
    with pytest.raises(ValueError, match="overlap"):
        controller.load_controller_config(save_config(config_file))


def test_commands_reuse_low_level_parser_without_per_tunnel_registration(config_file):
    path, _ = config_file
    config = controller.load_controller_config(path)
    commands = controller.component_commands(config)
    assert list(commands) == ["bgpls", "pcep", "dashboard"]
    for command in commands.values():
        assert command[:3] == [sys.executable, "-m", "ninja_pce.cli"]
        build_parser().parse_args(command[3:])
    listener = commands["pcep"]
    assert "--compute-domain-policy" in listener
    assert "--compute-delegated-tunnel" not in listener
    assert "--compute-headend" not in listener
    assert "--allow-peer" not in listener
    plan = controller.controller_plan(config)
    assert plan["components"] == commands
    assert plan["config_digest"] == config.config_digest
    assert plan["writes_network_state"] is False


def test_shared_freshness_limit_reaches_collector_computer_and_dashboard(config_file, monkeypatch):
    from ninja_pce import pcep, pcep_compute, startup
    from ninja_pce.live import _serve_pcep

    path, document = config_file
    document["bgpls"]["max_age_seconds"] = 12
    config = controller.load_controller_config(save_config(config_file))
    commands = controller.component_commands(config)
    assert commands["bgpls"][commands["bgpls"].index("--max-age") + 1] == "12.0"
    assert commands["dashboard"][commands["dashboard"].index("--max-age") + 1] == "12.0"
    received = {}
    original_computer = pcep_compute.PcepPathComputer

    def computer(**kwargs):
        received.update(kwargs)
        return original_computer(**kwargs)

    class Server:
        addresses = ()

        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            pass

        async def serve_forever(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(pcep_compute, "PcepPathComputer", computer)
    monkeypatch.setattr(pcep, "PcepServer", Server)
    monkeypatch.setattr(startup, "require_available_listener", lambda *args, **kwargs: None)
    args = build_parser().parse_args(commands["pcep"][3:])
    assert asyncio.run(_serve_pcep(args)) == 0
    assert received["max_age_seconds"] == 12.0
    assert received["domain_policy"].max_bandwidth_bps == 100_000_000


@pytest.mark.parametrize(("value", "expected", "kind"), [("30", 30, int), ("30.0", 30, int),
                                                        ("12.5", 12.5, float)])
def test_cli_freshness_keeps_historic_integer_policy_identity(value, expected, kind):
    args = build_parser().parse_args(["pcep-listen", "--capture", "unused.json", "--compute-max-age", value])
    assert args.compute_max_age == expected and type(args.compute_max_age) is kind
    defaults = build_parser().parse_args(["pcep-listen", "--capture", "unused.json"])
    assert defaults.compute_max_age == 30 and type(defaults.compute_max_age) is int


def test_wrapper_preserves_existing_managed_startup_digest_and_attempt_history(
    config_file, monkeypatch, offline_server,
):
    from test_pcep_startup_recovery import complete, startup_lab, startup_reply

    path, document = config_file
    lab = startup_lab(path.parent, monkeypatch)
    planner, computer, _, _ = lab
    complete(lab, startup_reply(lab))
    assert type(computer.max_age_seconds) is int
    assert planner.snapshot()["startup_recovery"]["total_attempts"] == 1
    original_digest = planner.snapshot()["startup_recovery"]["policy_digest"]
    protected = (planner.path, planner.operation_journal,
                 planner.operation_journal.parent / ".ninja-pce-reservations.json")
    original_bytes = {item: item.read_bytes() for item in protected}
    (path.parent / "nodes.json").write_text(json.dumps(computer.node_names))
    document["files"].update(bgpls_raw=str(computer.raw_path), bgpls_status=str(computer.status_path),
                              topology=str(computer.topology_path))
    document["bgpls"].update(required_peers=list(computer.required_peers), node_map="nodes.json")
    document["pcep"].update(allowed_peers=list(computer.headends), headends=computer.headends,
                            provisioning_state=str(planner.operation_journal), managed_recovery={
                                "operation_id": planner.operation_id, "journal_path": str(planner.path),
                                "startup_recovery": True})
    config = controller.load_controller_config(save_config(config_file))
    listener = controller.component_commands(config, ["pcep"])["pcep"]
    assert listener[listener.index("--compute-max-age") + 1] == "30.0"
    assert main(listener[3:]) == 0
    reopened = offline_server["options"]["managed_failover"]
    assert type(reopened.computer.max_age_seconds) is int
    assert reopened.validate_authority()
    assert reopened.snapshot()["startup_recovery"]["policy_digest"] == original_digest
    assert reopened.snapshot()["startup_recovery"]["total_attempts"] == 1
    assert reopened.snapshot()["attempts"] == 2
    assert {item: item.read_bytes() for item in protected} == original_bytes


def test_legacy_explicit_bindings_and_recovery_paths_are_preserved(config_file):
    path, document = config_file
    (path.parent / "nodes.json").write_text(json.dumps({"192.0.2.1": "edge1"}))
    document["bgpls"]["node_map"] = "nodes.json"
    document["pcep"].update(allowed_peers=["192.0.2.1"], headends={"192.0.2.1": "192.0.2.1"},
                            delegated_tunnels={"192.0.2.1": [100]},
                            failover={"peer": "192.0.2.1", "tunnel_id": 100, "state_path": "recovery/legacy.json"})
    config = controller.load_controller_config(save_config(config_file))
    listener = controller.component_commands(config, ["pcep"])["pcep"]
    assert listener[listener.index("--compute-headend") + 1] == "192.0.2.1=192.0.2.1"
    assert listener[listener.index("--compute-delegated-tunnel") + 1] == "192.0.2.1=100"
    assert listener[listener.index("--failover-state") + 1] == str(path.parent / "recovery/legacy.json")
    assert not (path.parent / "recovery").exists()


@pytest.mark.parametrize("identifier", [0, -1, True, 65536])
def test_legacy_tunnel_ids_match_protocol_authorization_range(config_file, identifier):
    path, document = config_file
    (path.parent / "nodes.json").write_text(json.dumps({"192.0.2.1": "edge1"}))
    document["bgpls"]["node_map"] = "nodes.json"
    document["pcep"].update(allowed_peers=["192.0.2.1"], headends={"192.0.2.1": "192.0.2.1"},
                            delegated_tunnels={"192.0.2.1": [identifier]})
    with pytest.raises(ValueError, match="tunnel ID"):
        controller.load_controller_config(save_config(config_file))


def test_managed_configuration_validation_preserves_owned_journals_and_argv(config_file):
    path, document = config_file
    (path.parent / "nodes.json").write_text(json.dumps({"192.0.2.1": "edge1"}))
    document["bgpls"]["node_map"] = "nodes.json"
    identifier = "a" * 32
    provisioning = path.parent / "state/provisioning.json"
    write_state(provisioning, {"kind": "cli_tunnel_provisioning", "operations": {
        identifier: {"id": identifier, "status": "VERIFIED"}}})
    document["pcep"].update(allowed_peers=["192.0.2.1"], headends={"192.0.2.1": "192.0.2.1"},
                            provisioning_state="state/provisioning.json", managed_recovery={
                                "operation_id": identifier, "journal_path": "recovery/managed.json",
                                "startup_recovery": True})
    before = provisioning.read_bytes()
    config = controller.load_controller_config(save_config(config_file))
    listener = controller.component_commands(config, ["pcep"])["pcep"]
    assert listener[listener.index("--managed-failover-operation") + 1] == identifier
    assert listener[listener.index("--managed-failover-state") + 1] == str(path.parent / "recovery/managed.json")
    assert "--managed-startup-recovery" in listener
    assert provisioning.read_bytes() == before
    assert not (path.parent / "recovery").exists()
    document["pcep"]["managed_recovery"]["journal_path"] = "state/managed.json"
    with pytest.raises(ValueError, match="separate directory"):
        controller.load_controller_config(save_config(config_file))


def test_existing_recovery_checksum_is_validated_without_mutation(config_file):
    path, document = config_file
    (path.parent / "nodes.json").write_text(json.dumps({"192.0.2.1": "edge1"}))
    document["bgpls"]["node_map"] = "nodes.json"
    recovery = path.parent / "recovery.json"
    recovery.write_text('{"version": 2, "checksum": "invalid"}')
    document["pcep"].update(allowed_peers=["192.0.2.1"], headends={"192.0.2.1": "192.0.2.1"},
                            delegated_tunnels={"192.0.2.1": [100]}, failover={
                                "peer": "192.0.2.1", "tunnel_id": 100, "state_path": "recovery.json"})
    before = recovery.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        controller.load_controller_config(save_config(config_file))
    assert recovery.read_bytes() == before


def test_component_selection_is_explicit_and_disabled_components_cannot_start(config_file):
    path, document = config_file
    document["dashboard"]["enabled"] = False
    config = controller.load_controller_config(save_config(config_file))
    assert list(controller.component_commands(config)) == ["bgpls", "pcep"]
    assert list(controller.component_commands(config, ["pcep"])) == ["pcep"]
    for invalid in (["pcep", "pcep"], ["dashboard"], ["shell"], []):
        with pytest.raises(ValueError):
            controller.component_commands(config, invalid)
    assert build_parser().parse_args(["compute", "--topology", "x", "--demand", "y"]).command == "compute"


def test_supervisor_reaps_siblings_when_a_child_exits_even_successfully(config_file):
    path, _ = config_file
    config = controller.load_controller_config(path)
    commands = {"bgpls": [sys.executable, "-c", "import time; time.sleep(60)"],
                "pcep": [sys.executable, "-c", "pass"]}
    assert asyncio.run(controller._supervise(config, commands)) == 1
    for component in commands:
        record = load_state(controller._runtime_path(config, component))
        assert record["state"] == "stopped"
        assert controller._process_identity(record["child"]["pid"]) is None
    assert not (path.parent / "state").exists()


def test_partial_start_failure_terminates_and_reaps_started_child(config_file, monkeypatch):
    path, _ = config_file
    config = controller.load_controller_config(path)
    original = asyncio.create_subprocess_exec
    started = []

    async def start(*command, **kwargs):
        if started:
            raise OSError("second component cannot execute")
        process = await original(*command, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    commands = {component: [sys.executable, "-c", "import time; time.sleep(60)"] for component in ("bgpls", "pcep")}
    with pytest.raises(OSError, match="second component"):
        asyncio.run(controller._supervise(config, commands))
    assert len(started) == 1 and started[0].returncode is not None
    assert controller._process_identity(started[0].pid) is None


def test_sigterm_shutdown_kills_uncooperative_child_and_restores_handlers(config_file, monkeypatch):
    path, _ = config_file
    config = controller.load_controller_config(path)
    ready = path.parent / "child-ready"
    script = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    script += f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(60)"
    monkeypatch.setattr(controller, "_SHUTDOWN_SECONDS", 0.03)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    async def terminate_when_ready():
        for _ in range(300):
            if ready.exists():
                os.kill(os.getpid(), signal.SIGTERM)
                return
            await asyncio.sleep(0.005)
        raise AssertionError("Child did not become ready")

    async def exercise():
        result, _ = await asyncio.gather(controller._supervise(config, {"pcep": [sys.executable, "-c", script]}),
                                         terminate_when_ready())
        return result

    assert asyncio.run(exercise()) == 0
    record = load_state(controller._runtime_path(config, "pcep"))
    assert record["returncode"] == -signal.SIGKILL
    assert controller._process_identity(record["child"]["pid"]) is None
    assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())


def test_duplicate_component_supervisor_is_rejected_without_clobbering_owner(config_file):
    import fcntl

    path, _ = config_file
    config = controller.load_controller_config(path)
    runtime = Path(config.document["runtime_dir"])
    runtime.mkdir(parents=True)
    lock_path = runtime / ".ninja-pce-controller-pcep.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="already supervised"):
            asyncio.run(controller._supervise(config, {"pcep": [sys.executable, "-c", "pass"]}))
    assert not controller._runtime_path(config, "pcep").exists()


def synthetic_snapshot(*, pccs=1, source_state="fresh"):
    return {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "sources": [{"id": name, "state": source_state} for name in ("bgpls", "topology", "pcep")],
        "peers": [{"protocol": "PCEP", "fresh": True, "synchronized": True} for _ in range(pccs)],
        "summary": {"nodes": 2, "directed_links": 2, "pcep_peers": pccs, "active_lsps": 1},
    }


def test_status_separates_process_health_freshness_and_protocol_health(config_file, monkeypatch):
    from ninja_pce.dashboard import DashboardStore

    path, _ = config_file
    config = controller.load_controller_config(path)
    monkeypatch.setattr(DashboardStore, "snapshot", lambda *args: synthetic_snapshot())
    result, code = controller.controller_status(config)
    assert code == 1
    assert {process["state"] for process in result["processes"]} == {"unobserved"}
    assert result["protocol"]["fresh_synchronized_pccs"] == 1
    assert result["forwarding_verified"] is False
    monkeypatch.setattr(controller, "_runtime_status", lambda *args: {"component": args[1], "state": "running"})
    result, code = controller.controller_status(config)
    assert code == 0 and result["status"] == "healthy"
    monkeypatch.setattr(DashboardStore, "snapshot", lambda *args: synthetic_snapshot(source_state="stale"))
    assert controller.controller_status(config)[1] == 1
    monkeypatch.setattr(DashboardStore, "snapshot", lambda *args: synthetic_snapshot(pccs=0))
    result, code = controller.controller_status(config)
    assert code == 1 and "bootstrap" in result["message"]


def test_runtime_health_rejects_stopped_process_pid_reuse_and_config_changes(config_file):
    path, _ = config_file
    config = controller.load_controller_config(path)
    identity = {"pid": os.getpid(), "start_ticks": controller._process_identity(os.getpid())}
    record = {"kind": "controller_component", "component": "pcep", "config_digest": config.config_digest,
              "state": "running", "supervisor": identity, "child": identity, "updated_at": time.time()}
    runtime = controller._runtime_path(config, "pcep")
    write_state(runtime, record)
    assert controller._runtime_status(config, "pcep")["state"] == "running"
    reused = copy.deepcopy(record)
    reused["child"]["start_ticks"] = "different generation"
    write_state(runtime, reused)
    assert controller._runtime_status(config, "pcep")["state"] == "stopped"
    changed = {**record, "config_digest": "different config"}
    write_state(runtime, changed)
    assert controller._runtime_status(config, "pcep")["state"] == "config_mismatch"


def test_policy_changes_in_place_change_config_identity_and_block_stale_startup(config_file):
    path, _ = config_file
    config = controller.load_controller_config(path)
    identity = {"pid": os.getpid(), "start_ticks": controller._process_identity(os.getpid())}
    write_state(controller._runtime_path(config, "pcep"), {
        "kind": "controller_component", "component": "pcep", "config_digest": config.config_digest,
        "state": "running", "supervisor": identity, "child": identity})
    policy_path = path.parent / "domain.json"
    policy = json.loads(policy_path.read_text())
    policy["max_bandwidth_bps"] //= 2
    policy_path.write_text(json.dumps(policy))
    changed = controller.load_controller_config(path)
    assert changed.config_digest != config.config_digest
    assert controller._runtime_status(changed, "pcep")["state"] == "config_mismatch"
    with pytest.raises(ValueError, match="changed before"):
        asyncio.run(controller._supervise(config, {"pcep": [sys.executable, "-c", "pass"]}))
    assert load_state(controller._runtime_path(config, "pcep"))["state"] == "running"


def test_semantically_identical_policy_formatting_retains_config_identity(config_file):
    path, _ = config_file
    config = controller.load_controller_config(path)
    policy_path = path.parent / "domain.json"
    policy_path.write_text(json.dumps(json.loads(policy_path.read_text()), indent=4, sort_keys=True))
    assert controller.load_controller_config(path).config_digest == config.config_digest


def test_status_unavailable_bootstrap_does_not_create_files(config_file, capsys):
    path, _ = config_file
    assert main(["controller", "status", "--config", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "degraded" and result["forwarding_verified"] is False
    assert result["protocol"]["fresh_synchronized_pccs"] == 0
    assert not (path.parent / "state").exists()
    assert not (path.parent / "live").exists()


def test_version_is_available_without_configuration_or_live_dependencies(capsys):
    from ninja_pce import __version__

    with pytest.raises(SystemExit) as stopped:
        main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"ninja-pce {__version__}"
