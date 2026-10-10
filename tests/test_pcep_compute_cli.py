"""Offline CLI opt-in boundaries for path advice and initial delegation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ninja_pce import pcep
from ninja_pce.cli import main
from ninja_pce.pcep_compute import PcepPathComputer
from ninja_pce.pcep_delegation import DelegatedPathComputer

PEER = "10.255.0.1"
SECOND_PEER = "10.255.0.2"
EXPORTER = "172.16.100.102"


@pytest.fixture
def offline_server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Keep actual constructor validation, replacing all socket operations."""
    observed: dict[str, Any] = {}
    original = pcep.PcepServer

    class Server(original):
        addresses = [("172.16.100.10", 4189)]

        def __init__(self, config: pcep.PcepServerConfig, **options: Any):
            super().__init__(config, **options)
            observed.update(config=config, options=options, server=self)

        async def start(self) -> None:
            observed["started"] = True

        async def serve_forever(self) -> None:
            raise asyncio.CancelledError

        async def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(pcep, "PcepServer", Server)
    return observed


def base_args(tmp_path: Path) -> list[str]:
    return ["pcep-listen", "--listen-host", "172.16.100.10", "--allow-peer", PEER,
            "--capture", str(tmp_path / "pcep.json")]


def compute_options(tmp_path: Path) -> list[tuple[str, str]]:
    names = tmp_path / "nodes.json"
    names.write_text(json.dumps({PEER: "PE1", SECOND_PEER: "P1", "10.255.0.6": "PE3"}))
    return [("--compute-bgpls-raw", str(tmp_path / "raw.json")),
            ("--compute-bgpls-status", str(tmp_path / "status.json")),
            ("--compute-topology", str(tmp_path / "topology.json")),
            ("--compute-bgpls-peer", EXPORTER),
            ("--compute-node-map", str(names)),
            ("--compute-headend", f"{PEER}={PEER}")]


def flatten(options: list[tuple[str, str]]) -> list[str]:
    return [value for pair in options for value in pair]


@pytest.mark.parametrize("missing", range(6))
def test_incomplete_compute_options_cannot_start_listener(
    tmp_path: Path, offline_server: dict[str, Any], missing: int, capsys: pytest.CaptureFixture[str],
) -> None:
    options = compute_options(tmp_path)
    del options[missing]
    assert main([*base_args(tmp_path), *flatten(options)]) == 1
    assert "Computation requires" in capsys.readouterr().err
    assert not offline_server and not (tmp_path / "pcep.json").exists()


@pytest.mark.parametrize("binding", [f"{PEER}={PEER}", f"{PEER}={SECOND_PEER}",
                                      f"{SECOND_PEER}={SECOND_PEER}", "invalid", f"{PEER}=bad-ip"])
def test_duplicate_conflicting_or_unauthorized_headends_fail_closed(
    tmp_path: Path, offline_server: dict[str, Any], binding: str,
) -> None:
    assert main([*base_args(tmp_path), *flatten(compute_options(tmp_path)),
                 "--compute-headend", binding]) == 1
    assert not offline_server


def test_headend_source_must_exist_in_explicit_node_map(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    options = compute_options(tmp_path)
    options[-1] = ("--compute-headend", f"{PEER}=10.255.0.99")
    assert main([*base_args(tmp_path), *flatten(options)]) == 1
    assert not offline_server


@pytest.mark.parametrize("first,second", [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)])
def test_capture_and_live_input_path_collisions_are_rejected(
    tmp_path: Path, offline_server: dict[str, Any], first: int, second: int,
) -> None:
    options = compute_options(tmp_path)
    capture = str(tmp_path / "pcep.json")
    paths = [capture, options[0][1], options[1][1], options[2][1]]
    paths[second] = paths[first]
    options[:3] = [(options[index][0], paths[index + 1]) for index in range(3)]
    assert main([*base_args(tmp_path), *flatten(options)]) == 1
    assert not offline_server and not Path(capture).exists()


def test_capture_symlink_alias_of_raw_input_is_rejected(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    options = compute_options(tmp_path)
    raw = Path(options[0][1])
    raw.write_text("historical evidence")
    alias = tmp_path / "pcep.json"
    alias.symlink_to(raw)
    assert main([*base_args(tmp_path), *flatten(options)]) == 1
    assert not offline_server and raw.read_text() == "historical evidence"


def test_capture_cannot_replace_its_node_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    options = compute_options(tmp_path)
    names = Path(options[4][1])
    before = names.read_bytes()

    def forbidden_server(*args: Any, **kwargs: Any) -> None:
        pytest.fail("CLI must reject a capture/node-map collision before constructing the server")

    monkeypatch.setattr(pcep, "PcepServer", forbidden_server)
    assert main([*base_args(tmp_path), "--capture", str(names), *flatten(options)]) == 1
    assert "distinct" in capsys.readouterr().err
    assert names.read_bytes() == before


def test_delegation_cannot_enable_computation_implicitly(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    assert main([*base_args(tmp_path), "--compute-delegated-tunnel", f"{PEER}=101"]) == 1
    assert not offline_server


@pytest.mark.parametrize("binding", [f"{SECOND_PEER}=101", f"{PEER}=0", f"{PEER}=65536",
                                      f"{PEER}=-1", f"{PEER}=101.0", f"{PEER}=１０１", "invalid"])
def test_invalid_delegation_authorization_cannot_start_listener(
    tmp_path: Path, offline_server: dict[str, Any], binding: str,
) -> None:
    assert main([*base_args(tmp_path), *flatten(compute_options(tmp_path)),
                 "--compute-delegated-tunnel", binding]) == 1
    assert not offline_server


def test_duplicate_delegated_tunnel_is_rejected(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    assert main([*base_args(tmp_path), *flatten(compute_options(tmp_path)),
                 "--compute-delegated-tunnel", f"{PEER}=101",
                 "--compute-delegated-tunnel", f"{PEER}=101"]) == 1
    assert not offline_server


def test_stateless_and_delegated_peer_options_conflict(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    assert main([*base_args(tmp_path), *flatten(compute_options(tmp_path)),
                 "--stateless-peer", PEER, "--compute-delegated-tunnel", f"{PEER}=101"]) == 1
    assert not offline_server


def test_valid_computation_passes_authorized_objects_and_closes_cleanly(
    tmp_path: Path, offline_server: dict[str, Any], capsys: pytest.CaptureFixture[str],
) -> None:
    options = compute_options(tmp_path)
    assert main([*base_args(tmp_path), *flatten(options), "--stateless-peer", PEER]) == 0
    computer = offline_server["options"]["path_computer"]
    assert isinstance(computer, PcepPathComputer)
    assert computer.headends == {PEER: PEER}
    assert computer.required_peers == (EXPORTER,)
    assert computer.raw_path == Path(options[0][1])
    assert computer.status_path == Path(options[1][1])
    assert computer.topology_path == Path(options[2][1])
    assert computer.node_names == {PEER: "PE1", SECOND_PEER: "P1", "10.255.0.6": "PE3"}
    assert offline_server["config"].stateless_peers == (PEER,)
    assert "delegated_computer" not in offline_server["options"]
    assert offline_server["started"] and offline_server["closed"]
    assert json.loads(capsys.readouterr().out)["mode"] == "request_computation"


def test_valid_delegation_binds_only_explicit_headend_and_tunnels(
    tmp_path: Path, offline_server: dict[str, Any],
) -> None:
    assert main([*base_args(tmp_path), "--allow-peer", SECOND_PEER,
                 *flatten(compute_options(tmp_path)), "--stateless-peer", SECOND_PEER,
                 "--compute-delegated-tunnel", f"{PEER}=101",
                 "--compute-delegated-tunnel", f"{PEER}=102"]) == 0
    options = offline_server["options"]
    delegated = options["delegated_computer"]
    assert isinstance(delegated, DelegatedPathComputer)
    assert delegated.computer is options["path_computer"]
    assert delegated.allowed_tunnels == {PEER: (101, 102)}
    assert delegated.computer.headends == {PEER: PEER}
    assert offline_server["config"].allowed_peers == (PEER, SECOND_PEER)
    assert offline_server["config"].stateless_peers == (SECOND_PEER,)
    assert offline_server["started"] and offline_server["closed"]
    assert not (tmp_path / "pcep.json").exists()
