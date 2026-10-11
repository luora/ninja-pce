"""Bounded one-tunnel failure recovery; historical graphs, no device sockets."""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_pcep_compute import EXPORTER, FIXTURES, NAMES, PEER, prepare

import ninja_pce.pcep_compute as computation
from ninja_pce.bgpls import parse_gobgp_snapshot, topology_document
from ninja_pce.cspf import CspfNoPath
from ninja_pce.pcep import parse_reports
from ninja_pce.pcep_failover import FailureRecoveryPlanner
from ninja_pce.state import load_state, write_state

PCEP = Path(__file__).parent / "fixtures/pcep-iosxe-16.9.7"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    computer = prepare(tmp_path, monkeypatch)
    clock = [datetime.fromisoformat(json.loads(computer.raw_path.read_text())["captured_at"]).timestamp()]
    monkeypatch.setattr(computation, "_utcnow", lambda: datetime.fromtimestamp(clock[0], timezone.utc))
    wire = bytes.fromhex((PCEP / "tunnel100-computed-active.hex").read_text())
    report = parse_reports(wire[4:], allow_delegation=True)[0]
    report.update(session_id="session-one", received_at=clock[0], stale=False)
    planner = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100,
                                     journal_path=tmp_path / "failure.json", clock=lambda: clock[0])
    return planner, computer, clock, report


def _publish(computer, clock, filename="bgpls-withdrawn-raw.json"):
    # Simulate a new publication using captured graph bytes with an explicit
    # test clock; immutable source fixtures are never rewritten.
    capture = json.loads((FIXTURES / filename).read_text())
    capture["captured_at"] = datetime.fromtimestamp(clock[0], timezone.utc).isoformat()
    capture["poll_started_at"] = capture["captured_at"]
    topology = parse_gobgp_snapshot(capture, required_peers=(EXPORTER,), node_names=NAMES,
                                    now=datetime.fromtimestamp(clock[0], timezone.utc))
    computer.raw_path.write_text(json.dumps(capture))
    computer.topology_path.write_text(json.dumps(topology_document(topology)))
    write_state(computer.status_path, {"status": "ok", "observed_at": clock[0],
                                      "metadata": topology.raw_metadata, "nodes": len(topology.nodes),
                                      "directed_links": len(topology.links)})


def _observe(planner, report):
    planner.observe(PEER, report, fresh=True, synchronized=True)


def _adopt(lab):
    planner, _, _, report = lab
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["status"] == "MONITORING"


def _replacement(lab):
    planner, computer, clock, report = lab
    _adopt(lab)
    clock[0] += 1
    _publish(computer, clock)
    _observe(planner, report)
    assert planner.compute(PEER, report, 17) is None
    assert planner.snapshot()["status"] == "DEBOUNCING"
    clock[0] += 6
    _observe(planner, report)
    reply = planner.compute(PEER, report, 17)
    assert reply is not None
    return reply


def _progress(report, reply, now, *, active=True, srp=None):
    updated = copy.deepcopy(report)
    update = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    updated.update(ero=update["ero"], received_at=now,
                   srp_id=update["srp_id"] if srp is None else srp,
                   operational_code=2 if active else 0,
                   operational_state="ACTIVE" if active else "DOWN")
    return updated


def test_healthy_path_is_adopted_read_only_without_optimization(lab):
    planner, computer, _, report = lab
    _adopt(lab)
    before = [path.read_bytes() for path in (computer.raw_path, computer.status_path, computer.topology_path)]
    assert planner.compute(PEER, report, 2) is None
    state = planner.snapshot()
    assert len(state["baseline"]["edges"]) == 3 and state["attempts"] == 0 and state["pending"] is None
    assert state["authoritative"] is state["reservation_created"] is False
    assert before == [path.read_bytes() for path in (computer.raw_path, computer.status_path, computer.topology_path)]


def test_withdrawal_returns_alternate_strict_path_preserving_constraints_and_records_intent(lab):
    planner, _, clock, report = lab
    reply = _replacement(lab)
    assert reply.outcomes[0]["status"] == "FAILURE_PATH"
    assert reply.outcomes[0]["path_nodes"] == ["PE1", "P3", "P4", "PE3"]
    assert reply.outcomes[0]["bandwidth_bps"] == 50_000_000
    assert len(reply.outcomes[0]["path_link_ids"]) == 3
    sent = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    original = {(a["class"], a["type"]): a["body_hex"] for a in report["attributes"]}
    attributes = {(a["class"], a["type"]): a["body_hex"] for a in sent["attributes"]}
    assert attributes[(5, 1)] == original[(5, 1)] and attributes[(9, 1)] == original[(9, 1)]
    assert sent["identifiers"] == report["identifiers"] and sent["delegate"]
    assert load_state(planner.path)["pending"] is None
    planner.prepare_send(PEER, report, reply)
    durable = load_state(planner.path)
    assert durable["attempts"] == 1 and durable["status"] == "AWAITING_REPORT"
    assert durable["pending"]["session_id"] == "session-one"
    assert durable["pending"]["created_at"] == clock[0]
    assert durable["pending"]["message_hex"] == reply.messages[0].hex()
    assert planner.compute(PEER, report, 18) is None


@pytest.mark.parametrize("missing", ["fresh", "sync", "delegation"])
def test_without_session_authority_no_baseline_or_update(lab, missing):
    planner, _, _, report = lab
    if missing == "delegation":
        report["delegate"] = False
    planner.observe(PEER, report, fresh=missing != "fresh", synchronized=missing != "sync")
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["baseline"] is None


@pytest.mark.parametrize("change", ["used_bw", "missing_used_bw", "protection", "metric_bound",
                                    "attribute", "tlv", "loose", "prefix", "rro"])
def test_unsupported_constraints_are_never_stripped_to_manufacture_a_path(lab, change):
    planner, _, _, report = lab
    if change == "used_bw":
        next(a for a in report["attributes"] if (a["class"], a["type"]) == (5, 5))["body_hex"] = "3f800000"
    elif change == "missing_used_bw":
        report["attributes"] = [a for a in report["attributes"] if (a["class"], a["type"]) != (5, 5)]
    elif change in {"protection", "metric_bound"}:
        attr = next(a for a in report["attributes"] if a["class"] == (9 if change == "protection" else 6))
        raw = bytearray.fromhex(attr["body_hex"])
        raw[14 if change == "protection" else 2] |= 1
        attr["body_hex"] = raw.hex()
    elif change == "attribute":
        report["attributes"].append({"class": 99, "type": 1, "body_hex": ""})
    elif change == "tlv":
        report["tlvs"].append({"type": 20, "value_hex": "00000001"})
    elif change == "loose":
        report["ero"][0]["loose"] = True
    elif change == "prefix":
        report["ero"][0]["prefix_length"] = 24
    else:
        report["rro"] = [{"type": 3, "raw_hex": "03040000"}]
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["baseline"] is None


def test_supported_matching_rro_is_observed_without_becoming_a_path_constraint(lab):
    planner, _, _, report = lab
    report["rro"] = [{key: value for key, value in hop.items() if key != "loose"} for hop in report["ero"]]
    report["attributes"].append({"class": 8, "type": 1,
                                  "body_hex": "".join(hop["raw_hex"] for hop in report["rro"])})
    reply = _replacement(lab)
    assert reply.outcomes[0]["status"] == "FAILURE_PATH"
    assert all(a["class"] != 8 for a in parse_reports(reply.messages[0][4:], allow_delegation=True)[0]["attributes"])


def test_adopted_path_can_recover_after_down_empty_ero_and_zero_actual_bandwidth(lab):
    planner, computer, clock, report = lab
    _adopt(lab)
    report.update(ero=[], operational_code=0, operational_state="DOWN")
    next(attr for attr in report["attributes"] if (attr["class"], attr["type"]) == (5, 5))["body_hex"] = "00000000"
    clock[0] += 1
    _publish(computer, clock)
    _observe(planner, report)
    assert planner.compute(PEER, report, 9) is None
    clock[0] += 6
    _observe(planner, report)
    reply = planner.compute(PEER, report, 9)
    assert reply is not None and reply.outcomes[0]["bandwidth_bps"] == 50_000_000
    sent = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    assert next(attr for attr in sent["attributes"] if (attr["class"], attr["type"]) == (5, 1))[
        "bits_per_second"] == 50_000_000
    assert not any((attr["class"], attr["type"]) == (5, 5) for attr in sent["attributes"])
    assert next(attr for attr in report["attributes"] if (attr["class"], attr["type"]) == (5, 5))[
        "body_hex"] == "00000000"


def test_down_empty_report_alone_never_establishes_a_baseline(lab):
    planner, _, _, report = lab
    report.update(ero=[], operational_code=0, operational_state="DOWN")
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["baseline"] is None


@pytest.mark.parametrize("broken", ["status", "feed", "stale"])
def test_stale_or_inconsistent_topology_never_establishes_failure(lab, broken):
    planner, computer, clock, report = lab
    _adopt(lab)
    if broken == "status":
        write_state(computer.status_path, {"status": "unavailable", "observed_at": clock[0]})
    elif broken == "feed":
        computer.topology_path.unlink()
    else:
        clock[0] += 31
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["pending"] is None
    assert planner.snapshot()["failure_since"] is None


def test_no_path_does_not_prepare_or_return_delegation(lab, monkeypatch):
    planner, _, _, report = lab
    _replacement(lab)

    def no_path(*_args, **_kwargs):
        raise CspfNoPath("No alternate admitted path")

    monkeypatch.setattr(computation, "compute_cspf", no_path)
    assert planner.compute(PEER, report, 2) is None
    assert planner.snapshot()["pending"] is None
    assert planner.snapshot()["status"] == "NO_REPLACEMENT"


def test_down_ack_is_progress_and_uncorrelated_up_cannot_clear_pending(lab):
    planner, _, clock, report = lab
    reply = _replacement(lab)
    planner.prepare_send(PEER, report, reply)
    zero = _progress(report, reply, clock[0], srp=0)
    _observe(planner, zero)
    assert planner.snapshot()["pending"] is not None
    down = _progress(report, reply, clock[0], active=False)
    _observe(planner, down)
    assert planner.snapshot()["pending"]["ack_seen"] is True
    assert planner.snapshot()["status"] == "AWAITING_REPORT"
    _observe(planner, zero)
    assert planner.snapshot()["pending"] is None and planner.snapshot()["status"] == "RECOVERED"


def test_old_active_zero_srp_report_during_pending_does_not_false_fail(lab):
    planner, _, clock, report = lab
    reply = _replacement(lab)
    planner.prepare_send(PEER, report, reply)
    old = copy.deepcopy(report)
    old.update(srp_id=0, received_at=clock[0])
    _observe(planner, old)
    _observe(planner, _progress(report, reply, clock[0], active=False))
    _observe(planner, old)
    assert planner.snapshot()["status"] == "AWAITING_REPORT" and not planner.snapshot()["blocked"]


@pytest.mark.parametrize("bad", ["ero", "identity", "error", "session"])
def test_wrong_terminal_evidence_retains_pending_for_manual_recovery(lab, bad):
    planner, _, clock, report = lab
    reply = _replacement(lab)
    planner.prepare_send(PEER, report, reply)
    terminal = _progress(report, reply, clock[0])
    if bad == "ero":
        terminal["ero"] = report["ero"]
    elif bad == "identity":
        terminal["identifiers"]["endpoint"] = "10.255.0.5"
    elif bad == "error":
        terminal["tlvs"].append({"type": 20, "value_hex": "00000001"})
    else:
        terminal["session_id"] = "session-two"
    _observe(planner, terminal)
    assert planner.snapshot()["pending"] is not None
    assert planner.snapshot()["status"] == "RECOVERY_REQUIRED"


def test_pending_timeout_and_restart_never_replay_even_matching_up_report(lab):
    planner, computer, clock, report = lab
    reply = _replacement(lab)
    planner.prepare_send(PEER, report, reply)
    restarted = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=planner.path,
                                       clock=lambda: clock[0])
    _observe(restarted, _progress(report, reply, clock[0]))
    assert restarted.compute(PEER, report, 18) is None
    assert restarted.snapshot()["status"] == "RECOVERY_REQUIRED"
    clock[0] += 31
    assert planner.snapshot()["status"] == "RECOVERY_REQUIRED"
    assert planner.snapshot()["pending"] is not None


def test_disconnect_and_new_session_require_a_new_healthy_graph_baseline(lab):
    planner, computer, clock, report = lab
    _adopt(lab)
    planner.failed("disconnect")
    assert planner.snapshot()["baseline"] is None
    saved = planner.path.stat().st_mtime_ns
    planner.failed("disconnect")
    assert planner.path.stat().st_mtime_ns == saved
    report["session_id"] = "session-two"
    clock[0] += 1
    _publish(computer, clock)
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["baseline"] is None


def test_restart_without_pending_does_not_reuse_a_historical_healthy_baseline(lab):
    planner, computer, clock, report = lab
    _adopt(lab)
    restarted = FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=planner.path,
                                       clock=lambda: clock[0])
    clock[0] += 1
    _publish(computer, clock)
    _observe(restarted, report)
    assert restarted.compute(PEER, report, 2) is None
    assert restarted.snapshot()["baseline"] is None


def test_changed_topology_before_send_rejects_write_intent(lab):
    planner, computer, clock, report = lab
    reply = _replacement(lab)
    clock[0] += 1
    _publish(computer, clock, "bgpls-healthy-raw.json")
    with pytest.raises(ValueError, match="topology"):
        planner.prepare_send(PEER, report, reply)
    assert planner.snapshot()["pending"] is None and planner.snapshot()["attempts"] == 0


def test_failure_debounce_resets_when_the_path_returns(lab):
    planner, computer, clock, report = lab
    _replacement(lab)
    clock[0] += 1
    _publish(computer, clock, "bgpls-healthy-raw.json")
    _observe(planner, report)
    assert planner.compute(PEER, report, 2) is None
    assert planner.snapshot()["failure_since"] is None
    clock[0] += 1
    _publish(computer, clock)
    _observe(planner, report)
    assert planner.compute(PEER, report, 3) is None
    assert planner.snapshot()["status"] == "DEBOUNCING"


@pytest.mark.parametrize("change", ["capacity", "unreserved"])
def test_bandwidth_changes_alone_never_trigger_recovery(lab, monkeypatch, change):
    planner, computer, clock, report = lab
    _adopt(lab)
    clock[0] += 1
    _publish(computer, clock, "bgpls-healthy-raw.json")
    original = computer._snapshot
    baseline_edges = planner.snapshot()["baseline"]["edges"]

    def changed_graph():
        topology, identity = original()
        changes = ({"reservable_bw_bps": 20_000_000, "max_bw_bps": 20_000_000,
                    "reserved_bw_bps": 0, "unreserved_bw_bps": (20_000_000,) * 8}
                   if change == "capacity" else {"reserved_bw_bps": 100_000_000,
                                                 "unreserved_bw_bps": (0,) * 8})
        return replace(topology, links=[replace(link, **changes) if link.id in baseline_edges else link
                                        for link in topology.links]), identity

    monkeypatch.setattr(computer, "_snapshot", changed_graph)
    _observe(planner, report)
    assert planner.compute(PEER, report, 1) is None
    assert planner.snapshot()["failure_since"] is None


@pytest.mark.parametrize("same_time", [True, False])
def test_failure_graph_must_be_newer_than_the_latest_healthy_baseline(lab, same_time):
    planner, computer, clock, report = lab
    _adopt(lab)
    first = clock[0]
    if not same_time:
        clock[0] += 10
        _publish(computer, clock, "bgpls-healthy-raw.json")
        _observe(planner, report)
        assert planner.compute(PEER, report, 1) is None
        clock[0] = first + 5
    _publish(computer, clock)
    clock[0] = first + 11
    _observe(planner, report)
    assert planner.compute(PEER, report, 2) is None
    clock[0] += 6
    _observe(planner, report)
    assert planner.compute(PEER, report, 3) is None
    assert planner.snapshot()["status"] == "TOPOLOGY_REPLAY"
    assert planner.snapshot()["pending"] is None


def test_known_correlated_success_stores_the_decision_topology_as_new_baseline(lab):
    planner, _, clock, report = lab
    reply = _replacement(lab)
    planner.prepare_send(PEER, report, reply)
    _observe(planner, _progress(report, reply, clock[0]))
    baseline = planner.snapshot()["baseline"]
    assert baseline["topology_digest"] == reply.topology_digest
    assert baseline["topology_timestamp"] == reply.outcomes[0]["topology_timestamp"]


def test_inherited_symbolic_name_remains_bound_when_later_report_omits_tlv17(lab):
    planner, _, _, report = lab
    report["tlvs"] = [item for item in report["tlvs"] if item["type"] != 17]
    reply = _replacement(lab)
    sent = parse_reports(reply.messages[0][4:], allow_delegation=True)[0]
    assert sent["symbolic_name_hex"] == report["symbolic_name_hex"]
    assert planner.snapshot()["baseline"]["symbolic_name_hex"] == report["symbolic_name_hex"]


def test_cooldown_and_three_write_budget_hold_across_successful_recoveries(lab, monkeypatch):
    planner, computer, clock, report = lab
    _adopt(lab)
    original = computer._snapshot
    disabled: set[str] = set()

    def failed_graph():
        topology, identity = original()
        return replace(topology, links=[replace(link, oper_up=False) if link.id in disabled else link
                                        for link in topology.links]), identity

    # This synthetic adapter models explicit operational-down evidence, while
    # retaining the actual graph and real CSPF/PCUpd implementation.
    monkeypatch.setattr(computer, "_snapshot", failed_graph)
    for attempt in range(1, 5):
        disabled.clear()
        disabled.add(planner.snapshot()["baseline"]["edges"][1])
        clock[0] += 1
        _publish(computer, clock, "bgpls-healthy-raw.json")
        _observe(planner, report)
        assert planner.compute(PEER, report, attempt) is None
        clock[0] += 6
        _observe(planner, report)
        reply = planner.compute(PEER, report, attempt)
        if attempt == 4:
            assert reply is None and planner.snapshot()["status"] == "BUDGET_EXHAUSTED"
            break
        if attempt > 1:
            assert reply is None and planner.snapshot()["status"] == "COOLDOWN"
            clock[0] += 30
            _publish(computer, clock, "bgpls-healthy-raw.json")
            _observe(planner, report)
            reply = planner.compute(PEER, report, attempt)
        assert reply is not None
        planner.prepare_send(PEER, report, reply)
        with pytest.raises(ValueError):
            planner.prepare_send(PEER, report, reply)
        report = _progress(report, reply, clock[0])
        _observe(planner, report)
        assert planner.snapshot()["pending"] is None
    assert load_state(planner.path)["attempts"] == 3


def test_another_journal_writer_prevents_overwriting_its_intent(lab):
    planner, _, _, report = lab
    reply = _replacement(lab)
    concurrent = load_state(planner.path)
    concurrent["reason"] = "another writer took ownership"
    write_state(planner.path, concurrent)
    with pytest.raises(ValueError, match="journal changed"):
        planner.prepare_send(PEER, report, reply)
    assert load_state(planner.path)["pending"] is None
    assert planner.snapshot()["blocked"] is True


def test_disconnect_while_worker_runs_invalidates_same_report_after_reappearance(lab, monkeypatch):
    planner, _, _, report = lab
    _replacement(lab)
    entered, release = threading.Event(), threading.Event()
    original = type(planner.delegated).__call__

    def blocked(self, *args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(planner.delegated), "__call__", blocked)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(planner.compute(PEER, report, 2)))
    worker.start()
    try:
        assert entered.wait(1)
        planner.failed("session temporarily unavailable")
        _observe(planner, report)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and outcomes == [None]
    assert planner.snapshot()["baseline"] is None and planner.snapshot()["pending"] is None


@pytest.mark.parametrize("corrupt", ["checksum", "baseline", "pending"])
def test_corrupt_or_malformed_journal_is_refused(lab, corrupt):
    planner, computer, clock, _ = lab
    _adopt(lab)
    if corrupt == "checksum":
        document = json.loads(planner.path.read_text())
        document["checksum"] = "0" * 64
        planner.path.write_text(json.dumps(document))
    else:
        document = load_state(planner.path)
        document[corrupt] = {}
        write_state(planner.path, document)
    with pytest.raises(ValueError):
        FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=planner.path,
                               clock=lambda: clock[0])


@pytest.mark.parametrize("option", [{"max_attempts": 4}, {"debounce_seconds": 0},
                                    {"cooldown_seconds": float("inf")}, {"ack_timeout_seconds": 301}])
def test_unbounded_or_invalid_policy_is_rejected(lab, option):
    planner, computer, _, _ = lab
    with pytest.raises(ValueError):
        FailureRecoveryPlanner(computer, peer=PEER, tunnel_id=100, journal_path=planner.path, **option)
