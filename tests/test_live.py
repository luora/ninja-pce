"""CLI publication, failure invalidation and shutdown contracts for live inputs."""
from __future__ import annotations

import asyncio
import json
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ninja_pce import bgpls, live, pcep
from ninja_pce.cli import main
from ninja_pce.loaders import load_topology
from ninja_pce.models import Topology
from ninja_pce.state import load_state

ROOT = Path(__file__).resolve().parents[1]


def collector_args(tmp_path):
    return [
        "bgpls-collect", "--proto", "unused.proto", "--peer", "172.16.100.102",
        "--output", str(tmp_path / "ted.json"), "--status", str(tmp_path / "status.json"),
        "--raw", str(tmp_path / "raw.json"),
    ]


def mock_collector(monkeypatch, *, capture=None, topology=None):
    capture = capture or (lambda: {"source": "synthetic-test"})
    topology = topology or load_topology(ROOT / "examples/topologies/simple_multiarea.yml")
    monkeypatch.setattr(bgpls, "GoBgpTopologyProvider", lambda **kwargs: SimpleNamespace(capture=capture))
    monkeypatch.setattr(bgpls, "parse_gobgp_snapshot", lambda *args, **kwargs: topology)
    return topology


def test_one_poll_publishes_loader_compatible_topology_and_separate_status(tmp_path, monkeypatch):
    expected = mock_collector(monkeypatch)
    assert main([*collector_args(tmp_path), "--once"]) == 0
    actual = load_topology(tmp_path / "ted.json")
    assert actual.nodes == expected.nodes
    # The file schema stores directional IDs; legacy shorthand base_id is an
    # internal normalization hint, not a persisted wire/source attribute.
    assert actual.links == [replace(edge, base_id=edge.id) for edge in expected.directional_links]
    assert load_state(tmp_path / "status.json")["status"] == "ok"
    assert json.loads((tmp_path / "raw.json").read_text()) == {"source": "synthetic-test"}


def test_failed_poll_invalidates_previously_published_topology(tmp_path, monkeypatch):
    def fail():
        raise bgpls.BgplsError("exporter disconnected")

    mock_collector(monkeypatch, capture=fail)
    (tmp_path / "ted.json").write_text("old snapshot")
    assert main([*collector_args(tmp_path), "--once"]) == 1
    assert not (tmp_path / "ted.json").exists()
    assert load_state(tmp_path / "status.json")["status"] == "unavailable"


def test_empty_live_rib_is_unavailable_instead_of_successful_unloadable_feed(tmp_path, monkeypatch):
    mock_collector(monkeypatch, topology=Topology(name="empty", nodes={}, links=[]))
    assert main([*collector_args(tmp_path), "--once"]) == 1
    assert not (tmp_path / "ted.json").exists()


def test_sigterm_removes_feed_and_restores_previous_signal_handler(tmp_path, monkeypatch):
    mock_collector(monkeypatch)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_seconds):
        assert load_topology(tmp_path / "ted.json").links
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(live.time, "sleep", terminate)
    assert main(collector_args(tmp_path)) == 0
    assert not (tmp_path / "ted.json").exists()
    assert load_state(tmp_path / "status.json")["status"] == "stopped"
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_poll_interval_does_not_start_collector(tmp_path, value):
    assert main([*collector_args(tmp_path), "--interval", value, "--once"]) == 1
    assert not (tmp_path / "ted.json").exists()


def test_overlapping_output_and_status_paths_are_rejected(tmp_path, monkeypatch):
    mock_collector(monkeypatch)
    assert main([*collector_args(tmp_path), "--status", str(tmp_path / "ted.json"), "--once"]) == 1
    assert not (tmp_path / "ted.json").exists()


def test_atomic_document_failure_preserves_previous_file(tmp_path):
    path = tmp_path / "document.json"
    path.write_text('{"previous": true}')
    with pytest.raises(ValueError):
        live._write_document(path, {"invalid": float("nan")})
    assert json.loads(path.read_text()) == {"previous": True}
    assert list(tmp_path.iterdir()) == [path]


def test_pcep_cli_passes_lab_address_allowlist_and_closes_on_cancellation(tmp_path, monkeypatch):
    observed = {}

    class Server:
        addresses = [("172.16.100.10", 4189)]

        def __init__(self, config, *, capture_path):
            observed.update(config=config, capture=capture_path)

        async def start(self):
            observed["started"] = True

        async def serve_forever(self):
            raise asyncio.CancelledError

        async def close(self):
            observed["closed"] = True

    monkeypatch.setattr(pcep, "PcepServer", Server)
    capture = tmp_path / "pcep.json"
    assert main(["pcep-listen", "--listen-host", "172.16.100.10", "--allow-peer", "10.255.0.1",
                 "--capture", str(capture)]) == 0
    assert observed["config"].listen_host == "172.16.100.10"
    assert observed["config"].allowed_peers == ("10.255.0.1",)
    assert observed["capture"] == capture
    assert observed["started"] and observed["closed"]
