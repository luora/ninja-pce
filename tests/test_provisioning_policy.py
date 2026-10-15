"""Domain selection and SSH identity enrollment remain independent."""
from __future__ import annotations

import asyncio
import base64
import copy
import hmac
import json
import os
import struct
from dataclasses import replace
from pathlib import Path

import pytest
from test_pcep_compute import prepare
from test_pcep_domain_policy import document

from ninja_pce import cli_provisioning, tunnel_api
from ninja_pce.cli_provisioning import _runtime_computer
from ninja_pce.models import ManagedDevice
from ninja_pce.pcep_domain_policy import DomainDelegationPolicy
from ninja_pce.provisioning_policy import (
    TransportEnrollmentRegistry,
    domain_destinations,
    domain_headends,
)


def public_key(byte: int = 1) -> str:
    fields = (b"ssh-ed25519", bytes([byte]) * 32)
    return base64.b64encode(b"".join(struct.pack("!I", len(value)) + value for value in fields)).decode()


def enrolled(tmp_path: Path, monkeypatch, *, host="192.0.2.40", port=22):
    device = ManagedDevice("management-profile", "PE1", host, "cisco_ios", "network-service",
                           port=port, password_env="NINJA_TEST_TRANSPORT_SECRET")
    monkeypatch.setenv(device.password_env, "synthetic-secret-never-in-policy")
    pins = tmp_path / "known_hosts"
    endpoint = host if port == 22 else f"[{host}]:{port}"
    pins.write_text(f"{endpoint} ssh-ed25519 {public_key()}\n")
    return TransportEnrollmentRegistry({"PE1": device}, pins), pins, device


def test_all_current_trusted_domain_routers_include_transit_without_inventory(tmp_path, monkeypatch):
    topology, _ = prepare(tmp_path, monkeypatch).validated_snapshot()
    policy = DomainDelegationPolicy.from_document(document())
    before = copy.deepcopy(topology)
    members = domain_headends(topology, policy)
    assert members == {name: node.loopback for name, node in topology.nodes.items()}
    assert "P1" in members and "P4" in members
    assert set(domain_destinations(topology, policy)) == set(topology.nodes)
    assert topology == before
    registry = TransportEnrollmentRegistry({}, None)
    assert registry.readiness("P1") == (False, "The domain headend has no enrolled configuration transport")
    assert "P1" in domain_headends(topology, policy)


def test_new_trusted_router_becomes_selectable_without_profile_registration(tmp_path, monkeypatch):
    topology, _ = prepare(tmp_path, monkeypatch).validated_snapshot()
    policy = DomainDelegationPolicy.from_document(document())
    original = topology.nodes["P1"]
    new = replace(original, id="new-transit", loopback="10.255.0.20")
    topology.nodes[new.id] = new
    detail = copy.deepcopy(topology.raw_metadata["bgpls_objects"][original.id])
    detail["identity"] += "-new"
    topology.raw_metadata["bgpls_objects"][new.id] = detail
    assert domain_headends(topology, policy)[new.id] == new.loopback
    assert new.id in domain_destinations(topology, policy)


def test_foreign_or_duplicate_router_identity_is_not_selectable(tmp_path, monkeypatch):
    topology, _ = prepare(tmp_path, monkeypatch).validated_snapshot()
    policy = DomainDelegationPolicy.from_document(document())
    original = topology.nodes["P1"]
    topology.nodes["duplicate"] = replace(original, id="duplicate")
    assert "P1" not in domain_headends(topology, policy)
    assert "duplicate" not in domain_destinations(topology, policy)
    topology.raw_metadata["bgpls_objects"]["PE1"]["instance_id"] = 88
    assert "PE1" not in domain_headends(topology, policy)


def test_enrollment_pins_management_endpoint_independently_of_te_router_id(tmp_path, monkeypatch):
    registry, pins, device = enrolled(tmp_path, monkeypatch)
    assert device.host == "192.0.2.40"
    assert registry.readiness("PE1") == (True, "")
    snapshot = registry.snapshot("PE1")
    assert snapshot["backend"] == "iosxe-ssh"
    assert snapshot["device"]["host"] == device.host
    assert snapshot["credential_reference"] == device.password_env
    assert len(snapshot["host_key_fingerprints"]) == 1
    assert "synthetic-secret-never-in-policy" not in json.dumps(snapshot)
    assert str(pins) not in json.dumps(snapshot)
    assert registry.readiness("P1")[0] is False


def test_key_rotation_requires_a_new_transport_revision(tmp_path, monkeypatch):
    registry, pins, device = enrolled(tmp_path, monkeypatch)
    revision = registry.revision("PE1")
    pins.write_text(pins.read_text() + f"192.0.2.99 ssh-ed25519 {public_key(9)}\n")
    assert registry.revision("PE1") == revision
    pins.write_text(f"{device.host} ssh-ed25519 {public_key(2)}\n")
    assert registry.revision("PE1") != revision
    pins.write_text(f"192.0.2.99 ssh-ed25519 {public_key(9)}\n")
    assert registry.readiness("PE1")[0] is False
    with pytest.raises(ValueError, match="no independently pinned"):
        registry.revision("PE1")
    assert registry.snapshot()["PE1"]["host_key_unavailable"] is True


def test_credential_rotation_does_not_change_reference_or_expose_secret(tmp_path, monkeypatch):
    registry, _, device = enrolled(tmp_path, monkeypatch)
    revision = registry.revision("PE1")
    monkeypatch.setenv(device.password_env, "replacement-secret")
    assert registry.revision("PE1") == revision
    monkeypatch.delenv(device.password_env)
    assert registry.readiness("PE1") == (False, "The enrolled transport credential is unavailable")
    assert registry.revision("PE1") == revision


def test_enrollment_copies_profiles_and_returns_detached_snapshots(tmp_path, monkeypatch):
    registry, pins, device = enrolled(tmp_path, monkeypatch)
    devices = {"PE1": device}
    registry = TransportEnrollmentRegistry(devices, pins)
    before = registry.revision("PE1")
    devices.clear()
    registry.devices.clear()
    snapshot = registry.snapshot("PE1")
    snapshot["device"]["host"] = "192.0.2.99"
    snapshot["host_key_fingerprints"].clear()
    assert registry.revision("PE1") == before


@pytest.mark.parametrize("first_host,second_host", [
    ("192.0.2.40", "192.0.2.40"),
    ("router.example.test", "ROUTER.EXAMPLE.TEST."),
    ("2001:db8::40", "2001:0db8:0:0:0:0:0:40"),
])
def test_distinct_te_namespaces_cannot_share_management_endpoint(tmp_path, monkeypatch, first_host, second_host):
    _, pins, device = enrolled(tmp_path, monkeypatch, host=first_host)
    other = replace(device, name="other-profile", topology_node_id="P1", host=second_host)
    with pytest.raises(ValueError, match="cannot share an enrolled management endpoint"):
        TransportEnrollmentRegistry({"PE1": device, "P1": other}, pins)


def test_different_management_ports_are_distinct_enrollment_endpoints(tmp_path, monkeypatch):
    _, pins, device = enrolled(tmp_path, monkeypatch)
    other = replace(device, name="other-profile", topology_node_id="P1", port=2222)
    registry = TransportEnrollmentRegistry({"PE1": device, "P1": other}, pins)
    assert set(registry.devices) == {"PE1", "P1"}


def test_management_port_requires_its_own_exact_pin(tmp_path, monkeypatch):
    registry, pins, device = enrolled(tmp_path, monkeypatch, port=2222)
    assert registry.readiness("PE1")[0] is True
    pins.write_text(f"{device.host} ssh-ed25519 {public_key()}\n")
    assert registry.readiness("PE1")[0] is False


def test_hashed_exact_management_pin_is_supported(tmp_path, monkeypatch):
    registry, pins, device = enrolled(tmp_path, monkeypatch)
    salt = bytes(range(20))
    hashed = hmac.digest(salt, device.host.encode(), "sha1")
    token = "|1|" + base64.b64encode(salt).decode() + "|" + base64.b64encode(hashed).decode()
    pins.write_text(f"{token} ssh-ed25519 {public_key()}\n")
    assert registry.readiness("PE1")[0] is True


@pytest.mark.parametrize("replacement", [
    "{host} ssh-rsa {key}\n", "{host} ssh-ed25519 invalid!\n",
    "@revoked {host} ssh-ed25519 {key}\n", "@cert-authority {host} ssh-ed25519 {key}\n",
    "*.example.test ssh-ed25519 {key}\n", "{host} ssh-ed25519\n",
])
def test_missing_malformed_wildcard_or_revoked_identity_blocks_only_transport(tmp_path, monkeypatch, replacement):
    registry, pins, device = enrolled(tmp_path, monkeypatch)
    pins.write_text(replacement.format(host=device.host, key=public_key()))
    assert registry.readiness("PE1")[0] is False
    assert set(registry.snapshot()) == {"PE1"}


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "oversized"])
def test_pin_file_requires_bounded_regular_direct_file(tmp_path, monkeypatch, kind):
    registry, pins, _ = enrolled(tmp_path, monkeypatch)
    if kind == "missing":
        pins.unlink()
    elif kind == "symlink":
        target = tmp_path / "actual-pins"
        pins.rename(target)
        pins.symlink_to(target)
    elif kind == "fifo":
        pins.unlink()
        os.mkfifo(pins)
    else:
        pins.write_bytes(b"#" * (1_048_576 + 1))
    assert registry.readiness("PE1")[0] is False


def test_enrollment_does_not_claim_other_rendered_platforms_are_qualified(tmp_path, monkeypatch):
    _, pins, device = enrolled(tmp_path, monkeypatch)
    for platform in ("cisco_xr", "juniper_junos"):
        registry = TransportEnrollmentRegistry({"PE1": replace(device, device_type=platform)}, pins)
        assert registry.readiness("PE1") == (
            False, "Configuration provisioning is qualified only for the Cisco IOS XE SSH profile")


def runtime_config(tmp_path):
    policy_path = tmp_path / "domain.json"
    policy_path.write_text(json.dumps(document()))
    return {"domain_policy": str(policy_path), "state_dir": str(tmp_path / "state"),
            "computer": {"raw_path": str(tmp_path / "raw.json"),
                         "status_path": str(tmp_path / "status.json"),
                         "topology_path": str(tmp_path / "topology.json"),
                         "required_peers": ["192.0.2.1"]}}


def test_runtime_domain_is_explicit_and_requires_no_per_router_mapping(tmp_path):
    config = runtime_config(tmp_path)
    before = copy.deepcopy(config)
    computer = _runtime_computer(config)
    assert computer.domain_policy.policy_digest == DomainDelegationPolicy.from_document(document()).policy_digest
    assert computer.headends == {} and computer.node_names == {}
    assert config == before
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("location", ["raw_path", "status_path", "topology_path", "inventory", "known_hosts",
                                      "pcep_capture", "socket_path", "state_dir", "runtime"])
def test_domain_policy_cannot_alias_observation_output_or_mutable_state(tmp_path, location):
    config = runtime_config(tmp_path)
    path = Path(config["domain_policy"])
    runtime = tmp_path / "runtime.json"
    if location in {"raw_path", "status_path", "topology_path"}:
        config["computer"][location] = str(path)
    elif location == "state_dir":
        config[location] = str(path.parent)
    elif location == "runtime":
        runtime = path
    else:
        config[location] = str(path)
    with pytest.raises(ValueError, match="distinct|mutable"):
        _runtime_computer(config, config_path=runtime)


@pytest.mark.parametrize("kind", ["null", "object", "yaml", "duplicate", "symlink", "hardlink"])
def test_runtime_domain_policy_rejects_implicit_or_aliased_sources(tmp_path, kind):
    config = runtime_config(tmp_path)
    path = Path(config["domain_policy"])
    if kind == "null":
        config["domain_policy"] = None
    elif kind == "object":
        config["domain_policy"] = document()
    elif kind == "yaml":
        config["domain_policy"] = str(path.with_suffix(".yaml"))
    elif kind == "duplicate":
        path.write_text('{"version": 1, "version": 1}')
    else:
        alias = tmp_path / "alias.json"
        alias.symlink_to(path) if kind == "symlink" else os.link(path, alias)
        config["pcep_capture"] = str(alias)
    with pytest.raises((ValueError, OSError)):
        _runtime_computer(config)


@pytest.mark.parametrize("kind", ["same", "hardlink", "symlink"])
def test_area_and_domain_policy_files_cannot_alias_each_other(tmp_path, kind):
    config = runtime_config(tmp_path)
    domain_path = Path(config["domain_policy"])
    area_path = tmp_path / "area.json"
    if kind == "same":
        area_path = domain_path
    elif kind == "hardlink":
        os.link(domain_path, area_path)
    else:
        area_path.symlink_to(domain_path)
    config["computer"]["ospf_multi_area_policy"] = str(area_path)
    with pytest.raises(ValueError, match="distinct"):
        _runtime_computer(config)


@pytest.mark.parametrize("enroll", [True, False])
@pytest.mark.parametrize("principal_policy", [None, "authenticated"])
def test_runtime_wires_domain_transports_authenticated_users_and_explicit_bounds(
        tmp_path, monkeypatch, enroll, principal_policy):
    config = runtime_config(tmp_path)
    config.update(origin="https://controller.example.test", proxy_uid=33, socket_path=str(tmp_path / "api.sock"),
                  pce_address="192.0.2.10", pcep_capture=str(tmp_path / "pcep.json"), max_outstanding=8,
                  tunnel_id_min=2000, tunnel_id_max=2999, max_operations=1024,
                  max_previews=128, max_journal_bytes=8_388_608)
    if principal_policy is not None:
        config["principal_policy"] = principal_policy
    if enroll:
        inventory = tmp_path / "inventory.json"
        inventory.write_text(json.dumps({"devices": [
            {"name": "first-profile", "topology_node_id": "PE1", "host": "192.0.2.40",
             "device_type": "cisco_ios", "username": "service", "password_env": "NINJA_FIRST"},
            {"name": "transit-profile", "topology_node_id": "P1", "host": "192.0.2.41",
             "device_type": "cisco_ios", "username": "service", "password_env": "NINJA_TRANSIT"}]}))
        pins = tmp_path / "known_hosts"
        pins.write_text(f"192.0.2.40 ssh-ed25519 {public_key()}\n192.0.2.41 ssh-ed25519 {public_key(2)}\n")
        config.update(inventory=str(inventory), known_hosts=str(pins))
    observed = {}

    class Backend:
        def __init__(self, *args, **kwargs):
            observed["backend_args"] = args
            observed["backend_kwargs"] = kwargs

        async def close(self):
            observed["backend_closed"] = True

    class API:
        def __init__(self, *args, **kwargs):
            observed["api_args"] = args
            observed["api_kwargs"] = kwargs

        async def start(self):
            observed["api_started"] = True

        async def close(self):
            observed["api_closed"] = True

    class Stopped:
        def set(self):
            pass

        async def wait(self):
            pass

    monkeypatch.setattr(cli_provisioning, "CliProvisioner", Backend)
    monkeypatch.setattr(cli_provisioning.asyncio, "Event", Stopped)
    monkeypatch.setattr(tunnel_api, "TunnelAPI", API)

    async def run():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)
        await cli_provisioning.serve(config)

    asyncio.run(run())
    assert set(observed["backend_args"][2]) == ({"PE1", "P1"} if enroll else set())
    assert observed["backend_args"][3] == ()
    kwargs = observed["backend_kwargs"]
    assert kwargs["domain_policy"].policy_digest == DomainDelegationPolicy.from_document(document()).policy_digest
    assert set(kwargs["transport_registry"].devices) == ({"PE1", "P1"} if enroll else set())
    assert all(kwargs[field] == config[field] for field in
               ("max_outstanding", "tunnel_id_min", "tunnel_id_max", "max_operations", "max_previews", "max_journal_bytes"))
    assert observed["api_args"][4] == ()
    assert observed["api_kwargs"]["principal_policy"] == (principal_policy or "authenticated")
    assert observed["api_started"] and observed["api_closed"] and observed["backend_closed"]
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("field", ["headends", "destinations"])
def test_domain_runtime_refuses_static_endpoint_allowlists(tmp_path, field):
    config = runtime_config(tmp_path)
    config[field] = ["PE1"]
    with pytest.raises(ValueError, match="discovers headends and destinations"):
        asyncio.run(cli_provisioning.serve(config))
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("fields", [
    {"principal_policy": "allowlist", "operators": ["legacy-operator"]},
    {"principal_policy": "allowlist"},
    {"principal_policy": "authenticated", "operators": ["legacy-operator"]},
    {"operators": ["legacy-operator"]},
])
def test_domain_runtime_refuses_additional_principal_allowlist(tmp_path, fields):
    config = {**runtime_config(tmp_path), **fields}
    with pytest.raises(ValueError, match="authenticated principals without an operator allowlist"):
        asyncio.run(cli_provisioning.serve(config))
    assert not (tmp_path / "state").exists()
