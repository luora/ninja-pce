"""Independent cross-module safety regressions; all device responses are synthetic."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ninja_pce.cspf import CspfNoPath, compute_cspf
from ninja_pce.loaders import load_demand, load_inventory, load_topology
from ninja_pce.models import Link, Node, ServiceDemand, Topology
from ninja_pce.reconcile import reconcile_once
from ninja_pce.reservations import ReservationLedger, authoritative_snapshot, resource_lock
from ninja_pce.southbound import build_push_plan, push_plan
from ninja_pce.verification import CliOperationalVerifier, VerificationExpectation

ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "examples/topologies/simple_multiarea.yml"
DEMAND = ROOT / "examples/demands/pe1_to_pe3_50m.yml"
INVENTORY = ROOT / "gns3/lab-data/inventory/gns3_inventory.yml"
FIXTURES = ROOT / "tests/fixtures/operational"


def two_node_ted(reserved=0, metadata=None, **edge_kwargs):
    return Topology("cross", {"A": Node("A", "192.0.2.1"), "B": Node("B", "192.0.2.2")},
                    [Link("wire", "A", "B", 1, 200, reserved, bidirectional=False, **edge_kwargs)], metadata or {})


def test_reflected_owners_cannot_each_claim_the_same_aggregate_debit(tmp_path):
    with resource_lock(tmp_path):
        ledger = ReservationLedger(tmp_path)
        for owner in ("A", "B"):
            result = compute_cspf(ledger.effective_topology(two_node_ted(), owner),
                                  ServiceDemand(owner, "A", "B", 80))
            ledger.reserve(owner, owner, result, "plan")
            ledger.transition(owner, "PUSHING")
        now = datetime.now(timezone.utc).isoformat()
        metadata = {"source": "collector", "timestamp": now, "complete": True, "authoritative": True,
                    "reservations": [{"lsp_id": owner, "owner": owner, "source": "collector", "observed_at": now,
                                      "status": "VERIFIED", "edges": ["wire"], "bandwidth_bps": 80_000_000}
                                     for owner in ("A", "B")]}
        contradictory = two_node_ted(80, metadata)
        with pytest.raises((ValueError, CspfNoPath)):
            effective = ledger.effective_topology(contradictory, "C")
            compute_cspf(effective, ServiceDemand("C", "A", "B", 100))


@pytest.mark.parametrize("alter", ["stale", "wrong_source", "future_relative_to_snapshot"])
def test_authoritative_global_metadata_cannot_mask_stale_directional_resource(alter):
    now = datetime.now(timezone.utc)
    metadata = {"source": "collector", "timestamp": now.isoformat(), "authoritative": True, "complete": True}
    timestamp = now - timedelta(seconds=1)
    source = "collector"
    if alter == "stale":
        timestamp -= timedelta(hours=1)
    elif alter == "wrong_source":
        source = "untrusted"
    else:
        timestamp += timedelta(seconds=2)
    ted = two_node_ted(metadata=metadata, timestamp=timestamp.isoformat(), provenance_source=source)
    assert not authoritative_snapshot(ted)


@pytest.mark.parametrize("flags", [
    {"execute": "false", "approved": "false"}, {"execute": 1, "approved": 1},
    {"execute": False, "approved": False, "force": "false"},
    {"execute": False, "approved": False, "require_authoritative": "false"},
])
def test_reconcile_execution_flags_must_be_boolean_before_any_network_call(tmp_path, flags):
    common = dict(topology_path=TOPOLOGY, demand_path=DEMAND, inventory_path=INVENTORY,
                  output_dir=tmp_path / "output", state_path=tmp_path / "state.json")
    preview = reconcile_once(**common)
    with pytest.raises(ValueError, match="boolean"):
        reconcile_once(**common, **flags, approved_plan_id=preview.plan_id,
                       environ={"NINJA_PCE_PE1_PASSWORD": "synthetic"},
                       connect_handler=lambda **kwargs: pytest.fail("Invalid flags must make zero network calls"))


def verify_fixture(fmt, text):
    identity = {"iosxr": "tunnel-te100", "iosxe": "Tunnel100", "junos": "lsp-customer-a-pe1-pe3"}[fmt]
    version = {"iosxr": "Cisco IOS XR Software, Version 7.9.2",
               "iosxe": "Cisco IOS XE Software, Version 17.12.04", "junos": "Junos: 23.4R2.13"}[fmt]
    expected = VerificationExpectation(identity, "10.255.0.6", 50_000_000,
                                       (("strict", "10.0.13.3"), ("strict", "10.0.34.4"), ("strict", "10.0.46.6")))
    command = f"show mpls lsp name {identity} extensive" if fmt == "junos" else "show mpls traffic-eng tunnels 100 detail"
    return CliOperationalVerifier().verify(fmt, version, {command: text}, expected)


@pytest.mark.parametrize(("fmt", "filename"), [
    ("iosxr", "iosxr_7.9_up.mock.txt"), ("iosxe", "iosxe_17.12_up.mock.txt"), ("junos", "junos_23.4_up.mock.txt"),
])
def test_duplicate_contradictory_operational_states_are_ambiguous(fmt, filename):
    text = (FIXTURES / filename).read_text()
    if fmt == "junos":
        text += "\nLSPstate: Down\n"
    else:
        text += "\nAdmin: up Oper: down Path: invalid Signalling: disconnected\n"
    assert not verify_fixture(fmt, text).verified


def test_duplicate_current_ero_and_junos_bandwidth_are_ambiguous():
    text = (FIXTURES / "iosxr_7.9_up.mock.txt").read_text()
    text += "\nCurrent LSP Info:\n Explicit Route:\n Strict, 10.0.0.99\n"
    assert not verify_fixture("iosxr", text).verified
    text = (FIXTURES / "junos_23.4_up.mock.txt").read_text() + "\nBandwidth: 40000000bps\n"
    assert not verify_fixture("junos", text).verified


class UnknownCommitConnection:
    def __init__(self, reply):
        self.reply = reply

    def send_command(self, command, **kwargs):
        if command == "show version":
            return "Cisco IOS XR Software, Version 7.9.2"
        if "traffic-eng tunnels" in command:
            return (FIXTURES / "iosxr_7.9_up.mock.txt").read_text()
        if "show route" in command:
            return "Routing entry for 10.255.0.6/32"
        return ""

    def send_config_set(self, commands, **kwargs):
        return "accepted"

    def commit(self, **kwargs):
        return self.reply

    def exit_config_mode(self):
        pass

    def disconnect(self):
        pass


@pytest.mark.parametrize("reply", ["", "unknown commit response", "timed out while waiting for commit"])
def test_operational_up_without_positive_commit_acceptance_never_verifies(reply):
    ted = load_topology(TOPOLOGY)
    request = load_demand(DEMAND)
    result = compute_cspf(ted, request)
    device = load_inventory(INVENTORY).device_for_node(request.source)
    plan = build_push_plan(result, ted, device)
    delivery = push_plan(plan, execute=True, approved=True, approved_plan_id=plan.plan_id,
                         environ={device.password_env: "synthetic"},
                         connect_handler=lambda **kwargs: UnknownCommitConnection(reply))
    assert not delivery.verified
