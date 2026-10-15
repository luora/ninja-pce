"""Per-operation authority retains recovery history across policy migration."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address

import pytest
from test_pcep_compute import PEER, request
from test_pcep_domain_integration import discovered
from test_pcep_domain_policy import report as domain_report
from test_pcep_managed_failover import managed_lab
from test_pcep_provisioning import setup
from test_pcep_startup_recovery import complete, startup_reply

from ninja_pce.pcep_delegation import DelegatedPathComputer
from ninja_pce.pcep_managed_failover import ManagedFailureRecoveryPlanner
from ninja_pce.pcep_provisioning import JournalProvisioningAuthorization
from ninja_pce.reservations import ReservationLedger
from ninja_pce.state import digest, load_state, write_state


def revised(journal, *, current=None):
    original = {"devices": {"PE1": {"host": PEER}}, "max_outstanding": 1}
    revision = digest(original)
    journal.update(provisioning_schema=2, policy_digest=revision, policies={revision: original})
    for operation in journal["operations"].values():
        operation["policy_revision"] = revision
    if current is not None:
        journal["policy_digest"] = digest(current)
        journal["policies"][digest(current)] = current
    return journal


def grow_independent_holds(ledger_path):
    ledger = load_state(ledger_path)
    for index in range(192):
        edges = [f"independent-{index}-edge-{edge}-" + "x" * 80 for edge in range(64)]
        ledger["entries"][f"independent-tunnel-{index}"] = {
            "owner": f"independent:{index}", "plan_id": digest({"independent": index}), "status": "VERIFIED",
            "current_bandwidth_bps": 1_000_000, "current_edges": edges,
            "resources": {edge: 1_000_000 for edge in edges}}
    write_state(ledger_path, ledger)
    assert 1_048_576 < ledger_path.stat().st_size < 16_777_216
    assert len(ReservationLedger(ledger_path.parent).entries) == 193


def test_policy_change_preserves_exact_historical_initial_authority(tmp_path, monkeypatch):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    authorization = copy.deepcopy(journal["operations"]["operation-1"]["authorization"])
    revised(journal, current={"devices": {}, "max_outstanding": 8})
    write_state(authorizer.path, journal)
    assert authorizer(PEER, report) == authorization["plan_id"]
    assert journal["operations"]["operation-1"]["authorization"] == authorization


def test_large_shared_ledger_preserves_exact_initial_authority_and_is_bounded(tmp_path, monkeypatch):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    revised(journal)
    write_state(authorizer.path, journal)
    ledger_path = tmp_path / ".ninja-pce-reservations.json"
    grow_independent_holds(ledger_path)
    assert authorizer(PEER, report) == journal["operations"]["operation-1"]["plan_id"]
    assert authorizer.owns(PEER, report)
    ledger_path.write_bytes(b" " * 16_777_217)
    assert authorizer(PEER, report) is None
    assert authorizer.owns(PEER, report)


@pytest.mark.parametrize("field", ["peer", "source_ip", "destination_ip"])
@pytest.mark.parametrize("representation", ["integer", "boolean", "null", "list"])
def test_nonstring_authorization_addresses_never_grant_initial_authority(
        tmp_path, monkeypatch, field, representation):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    revised(journal)
    authorization = journal["operations"]["operation-1"]["authorization"]
    address = authorization[field]
    authorization[field] = {"integer": int(IPv4Address(address)), "boolean": True,
                            "null": None, "list": [address]}[representation]
    journal["operations"]["operation-1"]["authorization_digest"] = digest(authorization)
    write_state(authorizer.path, journal)
    assert authorizer(PEER, report) is None


@pytest.mark.parametrize("field", ["setup_priority", "hold_priority"])
def test_float_priority_cannot_replace_exact_integer_authorization(tmp_path, monkeypatch, field):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    journal["operations"]["operation-1"]["authorization"][field] = 7.0
    journal["operations"]["operation-1"]["authorization_digest"] = digest(
        journal["operations"]["operation-1"]["authorization"])
    write_state(authorizer.path, journal)
    assert authorizer(PEER, report) is None


@pytest.mark.parametrize("change", ["missing_revision", "unknown_revision", "changed_policy", "wrong_schema"])
def test_missing_or_changed_operation_policy_grants_no_initial_authority(tmp_path, monkeypatch, change):
    _, report, journal, authorizer, _ = setup(tmp_path, monkeypatch)
    revised(journal)
    operation = journal["operations"]["operation-1"]
    if change == "missing_revision":
        del operation["policy_revision"]
    elif change == "unknown_revision":
        operation["policy_revision"] = "e" * 64
    elif change == "changed_policy":
        journal["policies"][operation["policy_revision"]]["max_outstanding"] = 8
    else:
        journal["provisioning_schema"] = 3
    write_state(authorizer.path, journal)
    assert authorizer(PEER, report) is None


def test_domain_creation_authority_accepts_transit_headend_without_static_registration(tmp_path, monkeypatch):
    _, original_report, journal, previous, _ = setup(tmp_path, monkeypatch)
    computer = discovered(tmp_path, monkeypatch)
    source = "10.255.0.5"
    computed = computer(source, request(source=source, bandwidth=125000,
                                       lspa=bytes(12) + bytes((7, 7, 0, 0))))
    assert computed.outcomes[0]["status"] == "PATH"
    operation = journal["operations"]["operation-1"]
    operation["headend"] = "P3"
    operation["reservation_id"] = "P3:iosxe:Tunnel20000"
    authorization = operation["authorization"]
    authorization.update(peer=source, source_ip=source, ero=computed.outcomes[0]["ero"],
                         path_edges=computed.outcomes[0]["path_link_ids"])
    operation.update(authorization_digest=digest(authorization), ero=authorization["ero"],
                     path_edges=authorization["path_edges"])
    revised(journal)
    ledger = load_state(tmp_path / ".ninja-pce-reservations.json")
    hold = ledger["entries"].pop("PE1:iosxe:Tunnel20000")
    hold.update(current_edges=authorization["path_edges"],
                resources={edge: 1_000_000 for edge in authorization["path_edges"]})
    ledger["entries"][operation["reservation_id"]] = hold
    write_state(tmp_path / ".ninja-pce-reservations.json", ledger)
    write_state(previous.path, journal)
    authorizer = JournalProvisioningAuthorization(previous.path, {}, domain_policy=computer.domain_policy)
    report = domain_report(source=source, tunnel=20000)
    report["attributes"] = copy.deepcopy(original_report["attributes"])
    delegated = DelegatedPathComputer(computer, {}, provisioning_authorizer=authorizer)
    assert authorizer(source, report) == authorization["plan_id"]
    result = delegated(source, [report], (17,))
    assert result.outcomes[0]["status"] == "INITIAL_PATH"
    assert result.outcomes[0]["provisioning_plan_id"] == authorization["plan_id"]
    assert computer.headends == {} and computer.node_names == {}
    topology, evidence = computer.validated_snapshot()
    missing = next(key for key, node in topology.nodes.items() if node.loopback == source)
    withdrawn = replace(topology, nodes={key: node for key, node in topology.nodes.items() if key != missing})
    monkeypatch.setattr(computer, "_snapshot", lambda: (withdrawn, evidence))
    refused = delegated(source, [report], (18,))
    assert refused.outcomes[0]["status"] == "DELEGATION_RETURNED"


@pytest.mark.parametrize("change", ["expired", "rollback", "corrupt", "missing"])
def test_configured_identity_cannot_fall_back_to_ordinary_domain_authority(tmp_path, monkeypatch, change):
    _, report, journal, original, _ = setup(tmp_path, monkeypatch)
    computer = discovered(tmp_path, monkeypatch)
    revised(journal)
    operation = journal["operations"]["operation-1"]
    if change == "expired":
        operation["authorization"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        operation["authorization_digest"] = digest(operation["authorization"])
    elif change == "rollback":
        operation["status"] = "ROLLBACK_REQUIRED"
    write_state(original.path, journal)
    if change == "corrupt":
        original.path.write_text("not valid state")
    elif change == "missing":
        original.path.unlink()
    authorizer = JournalProvisioningAuthorization(original.path, {}, domain_policy=computer.domain_policy)
    delegated = DelegatedPathComputer(computer, {}, provisioning_authorizer=authorizer)
    assert delegated.policy_allows(PEER, report)
    result = delegated(PEER, [report], (17,))
    assert result.outcomes[0]["status"] == "DELEGATION_RETURNED"


def test_new_configured_ownership_revokes_ordinary_domain_proposal_before_write(tmp_path, monkeypatch):
    _, report, journal, original, _ = setup(tmp_path, monkeypatch)
    computer = discovered(tmp_path, monkeypatch)
    revised(journal)
    pending = copy.deepcopy(journal)
    pending["operations"] = {}
    write_state(original.path, pending)
    authorizer = JournalProvisioningAuthorization(original.path, {}, domain_policy=computer.domain_policy)
    delegated = DelegatedPathComputer(computer, {}, provisioning_authorizer=authorizer)
    proposal = delegated(PEER, [report], (17,))
    assert proposal.outcomes[0]["status"] == "INITIAL_PATH"
    assert "domain_policy_digest" in proposal.outcomes[0]
    assert delegated.validate_authorization(PEER, report, proposal.outcomes[0])
    write_state(original.path, journal)
    assert not delegated.validate_authorization(PEER, report, proposal.outcomes[0])


@pytest.mark.parametrize("status", ["REJECTED", "ROLLED_BACK"])
@pytest.mark.parametrize("remaining", [None, "reservation", "owner", "missing_ledger", "corrupt_ledger"])
def test_released_tunnel_id_allows_manual_domain_reuse_only_after_both_holds_are_absent(
        tmp_path, monkeypatch, status, remaining):
    _, report, journal, original, _ = setup(tmp_path, monkeypatch)
    computer = discovered(tmp_path, monkeypatch)
    revised(journal)
    operation = journal["operations"]["operation-1"]
    operation["status"] = status
    write_state(original.path, journal)
    ledger_path = tmp_path / ".ninja-pce-reservations.json"
    ledger = load_state(ledger_path)
    hold = ledger["entries"].pop(operation["reservation_id"])
    if remaining == "reservation":
        hold["owner"] = "another-owner"
        ledger["entries"][operation["reservation_id"]] = hold
    elif remaining == "owner":
        ledger["entries"]["another-reservation"] = hold
    elif remaining == "corrupt_ledger":
        hold["owner"] = None
        ledger["entries"]["malformed-owner"] = hold
    write_state(ledger_path, ledger)
    if remaining == "missing_ledger":
        ledger_path.unlink()
    authorizer = JournalProvisioningAuthorization(original.path, {}, domain_policy=computer.domain_policy)
    delegated = DelegatedPathComputer(computer, {}, provisioning_authorizer=authorizer)
    result = delegated(PEER, [report], (17,))
    assert result.outcomes[0]["status"] == ("INITIAL_PATH" if remaining is None else "DELEGATION_RETURNED")
    if remaining is None:
        assert "domain_policy_digest" in result.outcomes[0]
        assert "provisioning_plan_id" not in result.outcomes[0]


def test_migration_and_new_policy_preserve_recovery_binding_budgets_and_hold(tmp_path, monkeypatch):
    policy = {"devices": {"PE1": {"host": PEER}}, "max_outstanding": 1}
    seed, computer, clock, report = managed_lab(tmp_path, monkeypatch, policy_document=policy)
    seed._data.update(attempts=2, last_attempt_at=clock[0] - 60)
    seed._save()
    planner = ManagedFailureRecoveryPlanner(
        computer, operation_journal=seed.operation_journal, operation_id=seed.operation_id,
        journal_path=seed.path, startup_recovery=True, clock=lambda: clock[0])
    report.update(ero=[], operational_code=0, operational_state="DOWN", srp_id=0)
    next(item for item in report["attributes"] if (item["class"], item["type"]) == (5, 5))["body_hex"] = bytes(4).hex()
    lab = planner, computer, clock, report
    complete(lab, startup_reply(lab))
    recovery_before = planner.path.read_bytes()
    ledger_before = (planner.operation_journal.parent / ".ninja-pce-reservations.json").read_bytes()
    binding_before = copy.deepcopy(planner._binding)
    journal = load_state(planner.operation_journal)
    operation_before = copy.deepcopy(journal["operations"][planner.operation_id])
    revised(journal, current={"devices": {}, "max_outstanding": 8})
    assert journal["operations"][planner.operation_id] == {**operation_before, "policy_revision": digest(policy)}
    write_state(planner.operation_journal, journal)
    assert planner.validate_authority()
    restarted = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, startup_recovery=True, clock=lambda: clock[0])
    assert restarted._binding == binding_before
    assert restarted.snapshot()["attempts"] == 2
    assert restarted.snapshot()["startup_recovery"]["total_attempts"] == 1
    assert planner.path.read_bytes() == recovery_before
    assert (planner.operation_journal.parent / ".ninja-pce-reservations.json").read_bytes() == ledger_before


def test_large_shared_v2_ledger_preserves_original_managed_authority_and_history(tmp_path, monkeypatch):
    policy = {"devices": {"PE1": {"host": PEER}}, "max_outstanding": 1}
    planner, computer, clock, _ = managed_lab(tmp_path, monkeypatch, policy_document=policy)
    planner._data.update(attempts=2, last_attempt_at=clock[0] - 60)
    planner._save()
    original_binding = copy.deepcopy(planner._binding)
    history_before = planner.path.read_bytes()
    journal = revised(load_state(planner.operation_journal), current={"devices": {}, "max_outstanding": 8})
    write_state(planner.operation_journal, journal)
    ledger_path = planner.operation_journal.parent / ".ninja-pce-reservations.json"
    original_hold = copy.deepcopy(load_state(ledger_path)["entries"]["PE1:iosxe:Tunnel1000"])
    grow_independent_holds(ledger_path)
    ledger_before = ledger_path.read_bytes()
    assert planner.validate_authority()
    restarted = ManagedFailureRecoveryPlanner(
        computer, operation_journal=planner.operation_journal, operation_id=planner.operation_id,
        journal_path=planner.path, clock=lambda: clock[0])
    assert restarted._binding == original_binding and restarted.snapshot()["attempts"] == 2
    assert load_state(ledger_path)["entries"]["PE1:iosxe:Tunnel1000"] == original_hold
    assert ledger_path.read_bytes() == ledger_before and planner.path.read_bytes() == history_before
    ledger_path.write_bytes(b" " * 16_777_217)
    assert not restarted.validate_authority()


@pytest.mark.parametrize("collision", [None, "reservation", "owner", "identity"])
def test_other_current_operations_do_not_rebind_managed_recovery(tmp_path, monkeypatch, collision):
    policy = {"devices": {"PE1": {"host": PEER}}, "max_outstanding": 1}
    planner, _, _, _ = managed_lab(tmp_path, monkeypatch, policy_document=policy)
    journal = revised(load_state(planner.operation_journal))
    operation = journal["operations"][planner.operation_id]
    identifier = "b" * 32
    other = copy.deepcopy(operation)
    other.update(id=identifier, owner="cli-provision:" + identifier,
                 reservation_id="PE1:iosxe:Tunnel1001", tunnel_id=1001)
    other["authorization"].update(tunnel_id=1001, symbolic_name="Tunnel1001_w")
    if collision == "reservation":
        other["reservation_id"] = operation["reservation_id"]
    elif collision == "owner":
        other["owner"] = operation["owner"]
    elif collision == "identity":
        other["authorization"]["tunnel_id"] = 1000
    other["authorization_digest"] = digest(other["authorization"])
    journal["operations"][identifier] = other
    ledger_path = planner.operation_journal.parent / ".ninja-pce-reservations.json"
    ledger = load_state(ledger_path)
    independent_hold = copy.deepcopy(ledger["entries"][operation["reservation_id"]])
    independent_hold["owner"] = "cli-provision:" + identifier
    ledger["entries"]["PE1:iosxe:Tunnel1001"] = independent_hold
    write_state(ledger_path, ledger)
    write_state(planner.operation_journal, journal)
    assert planner.validate_authority() is (collision is None)
