"""Synthetic GoBGP v3 API/RFC9552 wire fixtures; no router evidence or sockets."""
from __future__ import annotations

import base64
import copy
import json
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ninja_pce.bgpls import (
    BgplsError,
    GoBgpTopologyProvider,
    decode_json_stream,
    parse_gobgp_snapshot,
    topology_document,
)
from ninja_pce.cspf import compute_cspf
from ninja_pce.loaders import load_topology
from ninja_pce.models import ServiceDemand

NOW = datetime(2026, 10, 10, 20, tzinfo=timezone.utc)
PEER = "172.16.100.102"
AF = {"afi": "AFI_LS", "safi": "SAFI_LS"}
NAMES = {"10.255.0.1": "PE1", "10.255.0.6": "PE3"}
PROTO = Path(__file__).resolve().parents[1] / "gns3/controller/gobgp/gobgp-readonly.proto"


def tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("!HH", kind, len(value)) + value


def descriptor(router: str, *, area: int = 0) -> bytes:
    return (tlv(512, struct.pack("!I", 65000)) + tlv(513, bytes(4))
            + tlv(514, struct.pack("!I", area)) + tlv(515, ip_address(router).packed))


def node(router: str, *, area: int = 0) -> bytes:
    payload = bytes([3]) + bytes(8) + tlv(256, descriptor(router, area=area))
    return struct.pack("!HH", 1, len(payload)) + payload


def link(*, reverse: bool = False, local: str | None = None, instance: int = 0) -> bytes:
    source, target = ("10.255.0.6", "10.255.0.1") if reverse else ("10.255.0.1", "10.255.0.6")
    local = local or ("10.0.16.6" if reverse else "10.0.16.1")
    remote = "10.0.16.1" if reverse else "10.0.16.6"
    payload = (bytes([3]) + instance.to_bytes(8, "big") + tlv(256, descriptor(source))
               + tlv(257, descriptor(target)) + tlv(259, ip_address(local).packed)
               + tlv(260, ip_address(remote).packed))
    return struct.pack("!HH", 2, len(payload)) + payload


def te_attrs(*, metric: int = 10, maximum: float = 12_500_000,
             reservable: float = 12_500_000, vector: tuple[float, ...] | None = None) -> dict[int, bytes]:
    values = vector or tuple(12_500_000 - index * 1_250_000 for index in range(8))
    return {1088: struct.pack("!I", 5), 1089: struct.pack("!f", maximum),
            1090: struct.pack("!f", reservable), 1091: struct.pack("!8f", *values),
            1092: struct.pack("!I", metric), 1096: struct.pack("!II", 3, 19)}


def route(nlri: bytes, attrs: dict[int, bytes]) -> dict:
    value = b"".join(tlv(kind, value) for kind, value in sorted(attrs.items()))
    attribute = bytes([0x90, 29]) + struct.pack("!H", len(value)) + value
    return {"destination": {"prefix": "synthetic-display-key", "paths": [{
        "best": True, "family": AF.copy(), "neighborIp": PEER, "stale": False,
        "nlriBinary": base64.b64encode(nlri).decode(),
        "pattrsBinary": [base64.b64encode(attribute).decode()],
    }]}}


def peer() -> dict:
    return {"peer": {
        "state": {"neighborAddress": PEER, "sessionState": "ESTABLISHED", "adminState": "UP",
                  "flops": 1, "messages": {"received": {"update": "3", "withdrawUpdate": "0",
                                                           "withdrawPrefix": "0"}}},
        "timers": {"state": {"uptime": "2026-10-10T19:00:00Z"}},
        "gracefulRestart": {"peerRestarting": False, "localRestarting": False},
        "afiSafis": [{"state": {"family": AF.copy(), "enabled": True, "accepted": "3"},
                      "mpGracefulRestart": {"state": {"endOfRibReceived": True}}}],
    }}


def capture() -> dict:
    return {"schema_version": 1, "source": "gobgp-v3.37.0-grpc",
            "poll_started_at": (NOW - timedelta(seconds=1)).isoformat(), "captured_at": NOW.isoformat(),
            "peers_before": [peer()], "peers_after": [peer()],
            "rib": [route(node("10.255.0.1"), {1028: ip_address("10.255.0.1").packed}),
                    route(node("10.255.0.6"), {1028: ip_address("10.255.0.6").packed}),
                    route(link(), te_attrs())]}


def parse(value: dict):
    return parse_gobgp_snapshot(value, required_peers=(PEER,), node_names=NAMES, now=NOW)


def test_real_wire_shapes_convert_directional_te_data_without_symmetry():
    topology = parse(capture())
    assert set(topology.nodes) == {"PE1", "PE3"}
    assert len(topology.directional_links) == 1
    edge = topology.links[0]
    assert (edge.source, edge.target, edge.from_ip, edge.to_ip) == ("PE1", "PE3", "10.0.16.1", "10.0.16.6")
    assert edge.metric == 10
    assert edge.max_bw_bps == edge.reservable_bw_bps == 100_000_000
    assert edge.unreserved_bw_bps == tuple(100_000_000 - index * 10_000_000 for index in range(8))
    assert edge.reserved_bw_bps == 70_000_000
    assert edge.available_bps(0) == 30_000_000
    assert edge.admin_groups == ("bit-0", "bit-2")
    assert edge.srlg == ("3", "19")
    assert topology.raw_metadata["authoritative"] is False
    result = compute_cspf(topology, ServiceDemand(name="synthetic", source="PE1", destination="PE3",
                                                 bandwidth_mbps=20))
    assert result.explicit_hops[0].address == "10.0.16.6"


def test_reverse_and_parallel_links_keep_independent_identities_and_capacities():
    value = capture()
    value["rib"] += [route(link(reverse=True), te_attrs(metric=80, vector=(1_250_000,) * 8)),
                     route(link(local="10.0.26.1"), te_attrs(metric=7))]
    topology = parse(value)
    assert len({edge.id for edge in topology.links}) == 3
    reverse = next(edge for edge in topology.links if edge.source == "PE3")
    assert reverse.metric == 80 and reverse.available_bps() == 10_000_000


def test_reordering_descriptors_does_not_change_identity():
    first = parse(capture()).links[0].id
    value = capture()
    raw = link()
    # Swap the two interface descriptors only, retaining all information.
    changed = raw[:-16] + raw[-8:] + raw[-16:-8]
    value["rib"][-1] = route(changed, te_attrs())
    assert parse(value).links[0].id == first


@pytest.mark.parametrize("kind", [1088, 1089, 1090, 1091, 1092])
def test_missing_required_te_attribute_is_unknown_and_excluded(kind):
    value = capture()
    attrs = te_attrs()
    del attrs[kind]
    value["rib"][-1] = route(link(), attrs)
    topology = parse(value)
    assert topology.links == []
    assert topology.raw_metadata["complete"] is False
    assert f"Missing TLV {kind}" in topology.raw_metadata["excluded_objects"][0]["reason"]


def test_explicit_zero_metric_and_admin_group_are_preserved():
    value = capture()
    attrs = te_attrs(metric=0)
    attrs[1088] = bytes(4)
    value["rib"][-1] = route(link(), attrs)
    edge = parse(value).links[0]
    assert edge.metric == 0 and edge.admin_groups == ()


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1])
def test_invalid_float_bandwidth_is_excluded(amount):
    value = capture()
    value["rib"][-1] = route(link(), te_attrs(maximum=amount))
    assert parse(value).links == []


def test_fractional_byte_rate_capacity_is_rounded_down_in_bits():
    value = capture()
    value["rib"][-1] = route(link(), te_attrs(maximum=1.1, reservable=1.1, vector=(1.1,) * 8))
    assert parse(value).links[0].available_bps() == 8


def test_full_snapshot_withdrawal_removes_previous_objects():
    value = capture()
    assert len(parse(value).links) == 1
    value["rib"].pop()
    assert parse(value).links == []
    value["rib"] = []
    topology = parse(value)
    assert topology.nodes == {} and topology.links == []


@pytest.mark.parametrize("flag", ["stale", "isWithdraw", "filtered", "isNexthopInvalid"])
def test_unsafe_path_flag_rejects_entire_snapshot(flag):
    value = capture()
    value["rib"][-1]["destination"]["paths"][0][flag] = True
    with pytest.raises(BgplsError, match="Unsafe"):
        parse(value)


@pytest.mark.parametrize("mutation", ["down", "no_eor", "restart", "missing", "epoch", "updates"])
def test_session_health_and_quiescence_fail_closed(mutation):
    value = capture()
    selected = value["peers_after"][0]["peer"]
    if mutation == "down":
        selected["state"]["sessionState"] = "ACTIVE"
    elif mutation == "no_eor":
        selected["afiSafis"][0]["mpGracefulRestart"]["state"]["endOfRibReceived"] = False
    elif mutation == "restart":
        selected["gracefulRestart"]["peerRestarting"] = True
    elif mutation == "missing":
        value["peers_after"] = []
    elif mutation == "epoch":
        selected["timers"]["state"]["uptime"] = NOW.isoformat()
    else:
        selected["state"]["messages"]["received"]["update"] = "4"
    with pytest.raises(BgplsError):
        parse(value)


@pytest.mark.parametrize("seconds", [-1, 31])
def test_expired_or_future_capture_is_rejected(seconds):
    value = capture()
    value["captured_at"] = (NOW - timedelta(seconds=seconds)).isoformat()
    with pytest.raises(BgplsError):
        parse(value)


def test_duplicate_and_truncated_binary_fail_closed():
    value = capture()
    value["rib"].append(copy.deepcopy(value["rib"][-1]))
    with pytest.raises(BgplsError, match="Duplicate canonical"):
        parse(value)
    value = capture()
    value["rib"][-1] = route(link()[:-1], te_attrs())
    with pytest.raises(BgplsError, match="length"):
        parse(value)


def test_unknown_node_router_id_is_not_invented():
    value = capture()
    value["rib"][0] = route(node("10.255.0.1"), {1026: b"PE1"})
    topology = parse(value)
    assert "PE1" not in topology.nodes and topology.links == []


def test_output_roundtrips_through_current_yaml_loader(tmp_path):
    topology = parse(capture())
    path = tmp_path / "live.yml"
    path.write_text(yaml.safe_dump(topology_document(topology)))
    loaded = load_topology(path)
    assert loaded.nodes == topology.nodes
    assert loaded.links[0].unreserved_bw_bps == topology.links[0].unreserved_bw_bps
    assert loaded.links[0].id == topology.links[0].id
    assert loaded.raw_metadata["authoritative"] is False


def test_json_stream_rejects_duplicate_keys_and_truncation():
    assert decode_json_stream('{}\n {"peer":{}}') == [{}, {"peer": {}}]
    for text in ['{"peer":{},"peer":{}}', '{} {"peer":']:
        with pytest.raises(BgplsError):
            decode_json_stream(text)


def test_live_transport_uses_only_read_rpc_and_never_returns_cached_data():
    value = capture()
    replies = [value["peers_before"], value["rib"], value["peers_after"], []]
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write("\n".join(json.dumps(item) for item in replies.pop(0)).encode())
        return SimpleNamespace(returncode=0)

    provider = GoBgpTopologyProvider(proto_path=PROTO, required_peers=(PEER,), node_names=NAMES,
                                    runner=runner, clock=lambda: NOW)
    assert len(provider.snapshot().links) == 1
    assert [command[-1] for command in calls] == ["apipb.GobgpApi/ListPeer", "apipb.GobgpApi/ListPath",
                                               "apipb.GobgpApi/ListPeer"]
    assert json.loads(calls[1][calls[1].index("-d") + 1])["enableOnlyBinary"] is True
    with pytest.raises(BgplsError, match="missing"):
        provider.snapshot()


@pytest.mark.parametrize("mode", ["timeout", "failed", "truncated"])
def test_transport_failure_does_not_publish_partial_snapshot(mode):
    def runner(command, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        kwargs["stdout"].write(b'{"peer":')
        return SimpleNamespace(returncode=2 if mode == "failed" else 0)

    provider = GoBgpTopologyProvider(proto_path=PROTO, required_peers=(PEER,), runner=runner,
                                    clock=lambda: NOW)
    with pytest.raises(BgplsError):
        provider.snapshot()


def test_plaintext_endpoint_must_be_loopback():
    with pytest.raises(BgplsError, match="loopback"):
        GoBgpTopologyProvider(proto_path=PROTO, required_peers=(PEER,), endpoint="172.16.100.10:50051")
