"""Read-only dashboard truthfulness and HTTP boundaries; no router connections."""
from __future__ import annotations

import copy
import http.client
import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ninja_pce import dashboard
from ninja_pce.bgpls import parse_gobgp_snapshot, topology_document
from ninja_pce.cli import main
from ninja_pce.dashboard import DashboardConfig, DashboardStore, create_dashboard_server
from ninja_pce.pcep import encode_object, parse_objects, parse_reports
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import load_state, write_state

FIXTURES = Path(__file__).parent / "fixtures"
EXPORTER = "172.16.100.102"
PCC = "10.255.0.1"
NODE_NAMES = {"10.255.0.1": "PE1", "10.255.0.2": "P1", "10.255.0.3": "P3",
              "10.255.0.4": "P4", "10.255.0.5": "P2", "10.255.0.6": "PE3"}


def _config(directory: Path, **options: Any) -> DashboardConfig:
    return DashboardConfig(
        topology_path=directory / "topology.json", bgpls_status_path=directory / "bgpls-status.json",
        bgpls_raw_path=directory / "bgpls-raw.json", pcep_path=directory / "pcep.json",
        state_dir=directory / "state", host="127.0.0.1", port=0, **options,
    )


def _source(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    return next(source for source in snapshot["sources"] if source["id"] == name)


def _publish(directory: Path) -> tuple[DashboardConfig, datetime]:
    """Replay actual bytes at their historical clock, without rebasing them."""
    directory.mkdir(parents=True, exist_ok=True)
    config = _config(directory)
    raw = (FIXTURES / "bgpls-iosxe-16.9.7/bgpls-healthy-raw.json").read_bytes()
    capture = json.loads(raw)
    now = datetime.fromisoformat(capture["captured_at"])
    topology = parse_gobgp_snapshot(capture, required_peers=(EXPORTER,), node_names=NODE_NAMES, now=now)
    config.bgpls_raw_path.write_bytes(raw)
    config.topology_path.write_text(json.dumps(topology_document(topology)))
    write_state(config.bgpls_status_path, {
        "status": "ok", "observed_at": now.timestamp(), "nodes": len(topology.nodes),
        "directed_links": len(topology.links), "metadata": topology.raw_metadata,
    })
    wire = bytes.fromhex((FIXTURES / "pcep-iosxe-16.9.7/tunnel100-computed-active.hex").read_text())
    report = parse_reports(wire[4:], allow_delegation=True)[0]
    report.update(received_at=now.timestamp(), stale=False, authoritative=False)
    ids = report["identifiers"]
    key = f"{report['plsp_id']}:{ids['type']}:{ids['value_hex']}"
    write_state(config.pcep_path, {
        "kind": "passive_pcep_capture", "generated_at": now.timestamp(), "listener_running": True,
        "authoritative": False, "complete": False, "mode": "request_computation",
        "capabilities": {"stateful": True, "lsp_update": True, "lsp_initiation": False},
        "peers": {PCC: {"state": "ESTABLISHED", "fresh": True, "synchronized": True,
                        "last_received_at": now.timestamp(), "valid_until": now.timestamp() + 30,
                        "peer_open": {"stateful": True, "stateful_flags": 1},
                        "reports": {key: report}, "updates": {}}},
        "events": [],
    })
    return config, now


def test_missing_files_are_unavailable_and_do_not_create_state_directory(tmp_path: Path) -> None:
    directory = tmp_path / "not-created"
    snapshot = DashboardStore(_config(directory)).snapshot()
    assert snapshot["schema_version"] == 1 and snapshot["read_only"] is True
    assert snapshot["mode"] == "live"
    assert all(source["state"] in {"unavailable", "unconfigured"} for source in snapshot["sources"])
    assert snapshot["summary"]["nodes"] == snapshot["summary"]["active_lsps"] == 0
    assert snapshot["lsps"] == []
    assert not directory.exists()


def test_actual_historical_capture_projects_observations_at_its_own_clock(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "topology")["state"] == _source(snapshot, "bgpls")["state"] == "fresh"
    assert _source(snapshot, "pcep")["state"] == "fresh"
    assert snapshot["summary"]["nodes"] == 6 and snapshot["summary"]["directed_links"] == 16
    assert len(snapshot["topology"]["nodes"]) == 6 and len(snapshot["topology"]["links"]) == 16
    lsp = next(item for item in snapshot["lsps"] if item["tunnel_id"] == 100)
    assert lsp["reported_state"] == "ACTIVE" and lsp["fresh"] is True
    assert lsp["bandwidth_bps"] == 50_000_000
    assert lsp["ero"] == ["10.0.12.2", "10.0.25.5", "10.0.56.6"]
    assert lsp.get("forwarding_verified", False) is False
    assert "raw_hex" not in json.dumps(snapshot)


@pytest.mark.parametrize(("loose", "prefix_length"), [(True, 32), (False, 24)])
def test_ero_preserves_loose_and_nonhost_hops_for_unambiguous_path_display(
    tmp_path: Path, loose: bool, prefix_length: int,
) -> None:
    config, now = _publish(tmp_path)
    wire = bytes.fromhex((FIXTURES / "pcep-iosxe-16.9.7/tunnel100-computed-active.hex").read_text())
    encoded = []
    for obj in parse_objects(wire[4:]):
        body = bytearray(obj.body)
        if obj.object_class == 7:
            # Alter a real decoded ERO to a valid loose host or strict prefix.
            body[0] = 0x81 if loose else 1
            body[6] = prefix_length
        encoded.append(encode_object(obj.object_class, bytes(body), obj.object_type, flags=obj.flags))
    report = parse_reports(b"".join(encoded), allow_delegation=True)[0]
    report.update(received_at=now.timestamp(), stale=False, authoritative=False)
    capture = load_state(config.pcep_path)
    capture["peers"][PCC]["reports"] = {"reported-ero": report}
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    lsp = snapshot["lsps"][0]
    assert lsp["ero"][0] == "10.0.12.2"
    assert lsp["ero_hops"][0] == {"address": "10.0.12.2", "type": 1,
                                  "prefix_length": prefix_length, "loose": loose}
    assert "raw_hex" not in json.dumps(lsp)


@pytest.mark.parametrize("name", ["topology", "bgpls", "pcep", "ledger"])
def test_malformed_sources_are_invalid_without_crashing(tmp_path: Path, name: str) -> None:
    config, now = _publish(tmp_path)
    paths = {"topology": config.topology_path, "bgpls": config.bgpls_status_path,
             "pcep": config.pcep_path, "ledger": config.state_dir / ".ninja-pce-reservations.json"}
    paths[name].parent.mkdir(parents=True, exist_ok=True)
    paths[name].write_text('{"truncated":')
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, name)["state"] == "invalid"
    if name == "pcep":
        assert snapshot["lsps"] == [] and snapshot["summary"]["active_lsps"] == 0


@pytest.mark.parametrize("name", ["bgpls", "pcep", "ledger"])
def test_corrupt_controller_checksum_is_not_trusted(tmp_path: Path, name: str) -> None:
    config, now = _publish(tmp_path)
    path = {"bgpls": config.bgpls_status_path, "pcep": config.pcep_path,
            "ledger": config.state_dir / ".ninja-pce-reservations.json"}[name]
    if name == "ledger":
        write_state(path, {"entries": {}})
    document = json.loads(path.read_text())
    document["checksum"] = "0" * 64
    path.write_text(json.dumps(document))
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, name)["state"] == "invalid"


@pytest.mark.parametrize("malformed", ["peers", "report", "update", "event"])
def test_checksummed_but_malformed_nested_pcep_schema_is_invalid(tmp_path: Path, malformed: str) -> None:
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    if malformed == "peers":
        capture["peers"] = []
    elif malformed == "report":
        capture["peers"][PCC]["reports"] = {"malformed-report": []}
    elif malformed == "update":
        capture["peers"][PCC]["updates"] = {"1": []}
    else:
        capture["events"] = [[]]
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "pcep")["state"] == "invalid"
    assert snapshot["lsps"] == snapshot["computations"] == snapshot["events"] == []


@pytest.mark.parametrize("offset", [timedelta(seconds=31), timedelta(days=365), timedelta(seconds=-60)])
def test_historical_or_future_captures_never_become_fresh(tmp_path: Path, offset: timedelta) -> None:
    config, captured = _publish(tmp_path)
    snapshot = DashboardStore(config).snapshot(now=captured + offset)
    assert _source(snapshot, "topology")["state"] != "fresh"
    assert _source(snapshot, "bgpls")["state"] != "fresh"
    assert _source(snapshot, "pcep")["state"] != "fresh"
    assert not any(lsp["fresh"] for lsp in snapshot["lsps"])


@pytest.mark.parametrize("changed", ["status_timestamp", "feed_timestamp", "raw_timestamp", "graph_size",
                                    "feed_metric", "feed_source"])
def test_mismatched_collector_publications_are_not_live(tmp_path: Path, changed: str) -> None:
    config, now = _publish(tmp_path)
    other = (now - timedelta(seconds=1)).isoformat()
    if changed in {"status_timestamp", "graph_size"}:
        document = load_state(config.bgpls_status_path)
        if changed == "status_timestamp":
            document["metadata"]["timestamp"] = other
        else:
            document["directed_links"] = 15
        write_state(config.bgpls_status_path, document)
    else:
        path = config.topology_path if changed.startswith("feed_") else config.bgpls_raw_path
        document = json.loads(path.read_text())
        if changed == "feed_timestamp":
            document["metadata"]["timestamp"] = other
        elif changed == "feed_metric":
            document["links"][0]["metric"] += 1
        elif changed == "feed_source":
            document["metadata"]["source"] = "unrelated-source"
        else:
            document["captured_at"] = other
        path.write_text(json.dumps(document))
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "bgpls")["state"] != "fresh"
    assert _source(snapshot, "topology")["state"] != "fresh"


def test_known_failed_poll_invalidates_a_recent_retained_graph(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    write_state(config.bgpls_status_path, {"status": "unavailable", "observed_at": now.timestamp(),
                                         "error": "exporter disconnected"})
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "bgpls")["state"] != "fresh"
    assert _source(snapshot, "topology")["state"] != "fresh"


@pytest.mark.parametrize("observed", ["missing", "stale", "future", "boolean"])
def test_collector_status_requires_its_own_valid_fresh_observation_time(
    tmp_path: Path, observed: str,
) -> None:
    config, now = _publish(tmp_path)
    status = load_state(config.bgpls_status_path)
    if observed == "missing":
        status.pop("observed_at")
    elif observed == "stale":
        status["observed_at"] = now.timestamp() - 60
    elif observed == "future":
        status["observed_at"] = now.timestamp() + 60
    else:
        status["observed_at"] = True
    # The graph and metadata still agree; the status itself is no longer live.
    write_state(config.bgpls_status_path, status)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "bgpls")["state"] != "fresh"
    assert _source(snapshot, "topology")["state"] != "fresh"
    assert not any(peer["protocol"] == "BGP-LS" and peer["fresh"] for peer in snapshot["peers"])


def test_persisted_fresh_pcep_flag_expires_at_valid_until(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    capture["peers"][PCC]["valid_until"] = now.timestamp() - 1
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    peer = next(peer for peer in snapshot["peers"] if peer["protocol"] == "PCEP")
    assert peer["fresh"] is False
    assert all(not lsp["fresh"] for lsp in snapshot["lsps"])
    assert _source(snapshot, "pcep")["state"] != "fresh"


def test_removed_old_incarnation_is_never_counted_as_active(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    reports = capture["peers"][PCC]["reports"]
    old = copy.deepcopy(next(iter(reports.values())))
    old["removed"] = True
    old["identifiers"]["lsp_id"] = 223
    old["identifiers"]["value_hex"] = "0aff000100df00640aff00010aff0006"
    reports["old-removed"] = old
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert snapshot["summary"]["active_lsps"] == 1
    assert not any(item["operational_state"] in {"ACTIVE", "UP"} and item.get("removed", False)
                   for item in snapshot["lsps"])
    assert all(item["operational_state"] not in {"VERIFIED", "FORWARDING_VERIFIED"} for item in snapshot["lsps"])


def test_snapshot_is_read_only_and_never_creates_lock_or_temp_files(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    ledger = config.state_dir / ".ninja-pce-reservations.json"
    write_state(ledger, {"entries": {}})

    def contents() -> dict[str, tuple[bytes, int]]:
        return {str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in tmp_path.rglob("*") if path.is_file()}

    before = contents()
    DashboardStore(config).snapshot(now=now)
    DashboardStore(config).snapshot(now=now + timedelta(seconds=1))
    assert contents() == before
    assert not (config.state_dir / ".ninja-pce-resources.lock").exists()


def test_oversized_source_is_rejected_without_returning_its_payload(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    with config.pcep_path.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024 + 1)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "pcep")["state"] == "invalid"
    assert len(json.dumps(snapshot)) < 2 * 1024 * 1024
    assert snapshot["lsps"] == []


def test_fifo_observation_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    os.mkfifo(config.pcep_path)
    outcomes: list[dict[str, Any]] = []
    thread = threading.Thread(target=lambda: outcomes.append(DashboardStore(config).snapshot()), daemon=True)
    thread.start()
    thread.join(1)
    blocked = thread.is_alive()
    if blocked:
        # Unblock a regressed reader so this test does not leave a waiting thread.
        descriptor = os.open(config.pcep_path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(descriptor)
        thread.join(1)
    assert not blocked, "Dashboard source read waited for a FIFO writer"
    assert outcomes and _source(outcomes[0], "pcep")["state"] == "invalid"


def test_unrepresentable_ledger_bandwidth_is_invalid_without_crashing(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    write_state(config.state_dir / ".ninja-pce-reservations.json", {
        "entries": {"PE1:iosxe:Tunnel100": {"status": "APPLIED_UNVERIFIED",
                                           "current_bandwidth_bps": 10 ** 400, "current_edges": []}},
    })
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "ledger")["state"] == "invalid"
    assert snapshot["reservations"]["entries"] == []


def _reservation_entry() -> dict[str, Any]:
    return {"owner": "/controller/state/Tunnel100.json", "status": "APPLIED_UNVERIFIED",
            "current_bandwidth_bps": 50_000_000, "current_edges": ["new-edge"],
            "resources": {"old-edge": 80_000_000, "new-edge": 50_000_000}}


def test_ledger_displays_all_retained_resources_after_uncertain_reroute(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    entry = _reservation_entry()
    write_state(config.state_dir / ".ninja-pce-reservations.json", {
        "entries": {"PE1:iosxe:Tunnel100": entry},
    })
    # Exercise the actual core schema: old-edge remains held even though the
    # current desired path moved to new-edge and requested less bandwidth.
    assert ReservationLedger(config.state_dir).entries["PE1:iosxe:Tunnel100"] == entry
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "ledger")["state"] == "fresh"
    projected = snapshot["reservations"]["entries"][0]
    assert set(projected["edges"]) == {"old-edge", "new-edge"}
    assert projected["held_resources"] == entry["resources"]
    assert projected["current_edges"] == ["new-edge"]
    assert projected["current_bandwidth_bps"] == 50_000_000


@pytest.mark.parametrize("malformed", ["owner", "status", "resources", "undersized", "bandwidth"])
def test_invalid_core_reservation_schema_is_not_displayed_as_available_accounting(
    tmp_path: Path, malformed: str,
) -> None:
    config, now = _publish(tmp_path)
    entry = _reservation_entry()
    if malformed == "owner":
        entry.pop("owner")
    elif malformed == "status":
        entry["status"] = "RELEASED"
    elif malformed == "resources":
        entry.pop("resources")
    elif malformed == "undersized":
        entry["resources"]["new-edge"] = 1
    else:
        entry["current_bandwidth_bps"] = 50_000_000.5
    write_state(config.state_dir / ".ninja-pce-reservations.json", {
        "entries": {"PE1:iosxe:Tunnel100": entry},
    })
    with pytest.raises(ValueError):
        ReservationLedger(config.state_dir)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "ledger")["state"] == "invalid"
    assert snapshot["reservations"]["entries"] == []


def test_event_and_computation_projection_is_bounded_and_drops_raw_details(tmp_path: Path) -> None:
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    capture["events"] = [{"timestamp": now.timestamp() - index / 100, "message_name": "KEEPALIVE",
                          "peer": PCC, "direction": "rx", "raw_hex": "do-not-publish-raw-frames"}
                         for index in range(100)]
    capture["peers"][PCC]["updates"] = {
        str(index): {"sent_at": now.timestamp() - index / 100, "status": "INITIAL_PATH",
                     "reason": "do-not-publish-internal-details", "bandwidth_bps": 50_000_000}
        for index in range(100)
    }
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert len(snapshot["events"]) == len(snapshot["computations"]) == 80
    serialized = json.dumps(snapshot)
    assert "do-not-publish" not in serialized and "raw_hex" not in serialized


@pytest.mark.parametrize("exceeded", ["peers", "reports"])
def test_pcep_source_over_projection_limits_is_invalid(tmp_path: Path, exceeded: str) -> None:
    config, now = _publish(tmp_path)
    capture = load_state(config.pcep_path)
    peer = capture["peers"][PCC]
    if exceeded == "peers":
        capture["peers"] = {f"10.255.1.{index}": copy.deepcopy(peer) for index in range(1, 66)}
    else:
        report = next(iter(peer["reports"].values()))
        peer["reports"] = {str(index): copy.deepcopy(report) for index in range(513)}
    write_state(config.pcep_path, capture)
    snapshot = DashboardStore(config).snapshot(now=now)
    assert _source(snapshot, "pcep")["state"] == "invalid"
    assert snapshot["lsps"] == []


def test_demo_is_explicit_and_does_not_read_or_rebase_real_captures(tmp_path: Path, monkeypatch) -> None:
    config, captured = _publish(tmp_path)
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    live = DashboardStore(config).snapshot(now=now)
    assert live["mode"] == "live" and _source(live, "pcep")["state"] != "fresh"
    before = config.bgpls_raw_path.read_bytes(), config.pcep_path.read_bytes()
    guarded = {path.resolve() for path in
               (config.topology_path, config.bgpls_status_path, config.bgpls_raw_path, config.pcep_path)}
    original_open = Path.open

    def forbid_live_reads(path, *args, **kwargs):
        if path.resolve() in guarded:
            raise AssertionError("Demo attempted to read a real capture")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbid_live_reads)
        demo = DashboardStore(replace(config, demo=True)).snapshot(now=now)
    assert demo["mode"] == "demo" and demo["read_only"] is True
    assert before == (config.bgpls_raw_path.read_bytes(), config.pcep_path.read_bytes())
    assert json.loads(before[0])["captured_at"] == captured.isoformat()


@pytest.fixture
def dashboard_http(tmp_path: Path):
    server = create_dashboard_server(_config(tmp_path, demo=True))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def _request(server, path: str, *, method: str = "GET"):
    client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        client.request(method, path)
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


@pytest.mark.parametrize("path", ["/", "/index.html", "/app.js", "/styles.css", "/api/controller", "/healthz"])
def test_http_serves_only_fixed_resources_with_security_headers(dashboard_http, path: str) -> None:
    status, headers, body = _request(dashboard_http, path)
    assert status == 200 and body
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "'self'" in headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in headers["Content-Security-Policy"]
    if path == "/api/controller":
        assert json.loads(body)["read_only"] is True
        assert "no-store" in headers["Cache-Control"]


@pytest.mark.parametrize("path", ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/.git/config",
                                  "/api/file?path=/etc/passwd", "/captures/pcep.json", "/bgpls-raw.json"])
def test_http_rejects_traversal_and_arbitrary_capture_endpoints(dashboard_http, path: str) -> None:
    status, _headers, body = _request(dashboard_http, path)
    assert status == 404
    assert b"root:" not in body and b"nlriBinary" not in body


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
def test_http_mutating_and_unsupported_methods_are_405(dashboard_http, method: str) -> None:
    status, headers, _body = _request(dashboard_http, "/api/controller", method=method)
    assert status == 405
    assert "GET" in headers["Allow"] and "HEAD" in headers["Allow"]


def test_http_head_has_no_response_body(dashboard_http) -> None:
    status, headers, body = _request(dashboard_http, "/api/controller", method="HEAD")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


def test_cli_defaults_to_loopback_live_mode_and_closes_on_interrupt(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class Server:
        server_address = ("127.0.0.1", 8088)

        def serve_forever(self, **_options):
            raise KeyboardInterrupt

        def server_close(self):
            observed["closed"] = True

    def create(config):
        observed["config"] = config
        return Server()

    monkeypatch.setattr(dashboard, "create_dashboard_server", create)
    assert main(["dashboard"]) == 0
    assert observed["config"].host == "127.0.0.1" and observed["config"].port == 8088
    assert observed["config"].demo is False
    assert observed["closed"] is True


@pytest.mark.parametrize("arguments", [["--refresh", "0"], ["--refresh", "-1"], ["--refresh", "nan"],
                                       ["--refresh", "inf"], ["--max-age", "nan"],
                                       ["--port", "-1"], ["--port", "65536"]])
def test_cli_rejects_invalid_listener_options_before_binding(monkeypatch, arguments: list[str]) -> None:
    def forbid_binding(_config):
        raise AssertionError("Invalid dashboard options reached network binding")

    monkeypatch.setattr(dashboard, "create_dashboard_server", forbid_binding)
    assert main(["dashboard", *arguments]) == 1
