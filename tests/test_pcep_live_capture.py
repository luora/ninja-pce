"""Offline replay of an actual non-empty IOS XE 16.9.7 RSVP-TE PCRpt."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ninja_pce.pcep import PCRPT, PcepServer, PcepServerConfig, parse_reports

FIXTURE = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7/tunnel100-pcrpt.hex"
PEER = "10.255.0.1"
REPORT_KEY = "524388:18:0aff0001000100640aff00010aff0006"


def captured_report() -> dict[str, Any]:
    frame = bytes.fromhex(FIXTURE.read_text())
    assert hashlib.sha256(frame).hexdigest() == "b871d486e941beeea35690402c7677daa83abc56c4d43c56369f27cde45ad797"
    assert len(frame) == 156 and frame[1] == PCRPT
    assert int.from_bytes(frame[2:4], "big") == len(frame)
    reports = parse_reports(frame[4:])
    assert len(reports) == 1
    return reports[0]


def test_actual_tunnel100_identifiers_active_state_and_strict_path() -> None:
    report = captured_report()
    assert report["plsp_id"] == 524388
    assert report["symbolic_name"] == "Tunnel100_w"
    assert report["identifiers"]["type"] == 18
    assert report["identifiers"]["sender"] == "10.255.0.1"
    assert report["identifiers"]["endpoint"] == "10.255.0.6"
    assert report["identifiers"]["tunnel_id"] == 100
    assert report["identifiers"]["lsp_id"] == 1
    assert report["identifiers"]["extended_tunnel_id_hex"] == "0aff0001"
    assert report["identifiers"]["all_zero"] is False
    assert report["administrative_up"] is True
    assert report["operational_code"] == 2
    assert report["operational_state"] == "ACTIVE"
    assert report["sync"] is False and report["end_of_sync"] is False
    assert report["delegate"] is False and report["removed"] is False
    assert report["srp_id"] == 0
    assert [(hop["type"], hop["address"], hop["prefix_length"], hop["loose"])
            for hop in report["ero"]] == [
        (1, "10.0.13.3", 32, False),
        (1, "10.0.34.4", 32, False),
        (1, "10.0.46.6", 32, False),
    ]


def test_actual_tunnel100_bandwidth_priorities_and_extensions_are_preserved() -> None:
    report = captured_report()
    bandwidth = [item for item in report["attributes"] if (item["class"], item["type"]) == (5, 1)]
    assert len(bandwidth) == 1
    assert bandwidth[0]["bytes_per_second"] == 6_250_000
    assert bandwidth[0]["bits_per_second"] == 50_000_000
    assert bandwidth[0]["body_hex"] == "4abebc20"
    assert {"class": 5, "type": 5, "body_hex": "4abebc20"} in report["attributes"]
    lspa = [item for item in report["attributes"] if (item["class"], item["type"]) == (9, 1)]
    assert len(lspa) == 1
    body = bytes.fromhex(lspa[0]["body_hex"])
    assert len(body) == 16 and body[12:14] == bytes((7, 7))
    assert {"type": 31, "value_hex": "00000001c000"} in report["tlvs"]


def test_actual_report_applies_to_synchronized_peer_without_granting_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An in-memory session supplies liveness; replay opens no sockets and the
    # historical frame itself provides no present-day freshness evidence.
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    server = PcepServer(PcepServerConfig((PEER,)))
    state: dict[str, Any] = {
        "state": "ESTABLISHED", "source": f"pcep:{PEER}", "session_id": "offline-replay",
        "reports": {}, "lsp_names": {}, "synchronized": True,
    }
    server._peers[PEER] = state
    monkeypatch.setitem(server._connections, PEER, SimpleNamespace(last_rx=100.0, remote_deadtimer=120))
    server._apply_reports(state, [captured_report()])
    assert state["lsp_names"] == {
        "524388": {"name": "Tunnel100_w", "hex": "54756e6e656c3130305f77"},
    }
    snapshot = server.snapshot()
    peer = snapshot["peers"][PEER]
    assert peer["fresh"] is True and peer["synchronized"] is True
    assert set(peer["reports"]) == {REPORT_KEY}
    report = peer["reports"][REPORT_KEY]
    assert report["stale"] is False
    assert report["operational_state"] == "ACTIVE"
    assert report["source"] == f"pcep:{PEER}"
    assert report["session_id"] == "offline-replay"
    assert report["delegate"] is False and report["delegation_accepted"] is False
    assert report["authoritative"] is False and snapshot["authoritative"] is False
    assert snapshot["complete"] is False
    server._connections.clear()
    disconnected = server.snapshot()["peers"][PEER]
    assert disconnected["fresh"] is False
    assert disconnected["reports"][REPORT_KEY]["stale"] is True
