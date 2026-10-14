"""Bounded RFC 5440 path advice over a fresh, healthy BGP-LS observation.

PCRep is computation advice. This module never reserves capacity, credits an
existing LSP, signals RSVP, or grants PCEP delegation/update authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bgpls import BgplsError, OspfMultiAreaPolicy, parse_gobgp_snapshot, parse_ospf_multi_area_policy
from .cspf import CspfNoPath, CspfSearchLimit, compute_cspf
from .models import Affinity, PathConstraints, ServiceDemand, Topology, validate_identifier
from .pcep import (
    ERROR,
    PCREP,
    PcepObject,
    PcepProtocolError,
    encode_message,
    encode_object,
    parse_objects,
    parse_tlvs,
)
from .state import SCHEMA_VERSION, digest

if TYPE_CHECKING:
    from .pcep_domain_policy import DomainDelegationPolicy

MAX_FILE_BYTES = 1_048_576
MAX_REQUESTS = 8
MAX_OBJECTS = 512
MAX_NODES = 256
MAX_EDGES = 2048
SEARCH_BUDGET = 100_000


@dataclass(frozen=True)
class PathReply:
    messages: tuple[bytes, ...]
    outcomes: tuple[dict[str, Any], ...]
    topology_digest: str | None = None


@dataclass(frozen=True)
class _Request:
    request_id: int
    source: str
    destination: str
    bandwidth_bps: int
    bandwidth_wire: bytes
    setup_priority: int
    hold_priority: int
    affinity: Affinity
    max_metric: int | None
    supply_of: bool
    explicit_of: bool
    lsp_body: bytes | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _outcome(request_id: int | None, status: str, reason: str, **details: Any) -> dict[str, Any]:
    return {"request_id": request_id, "status": status, "reason": reason,
            "authoritative": False, "reservation_created": False, **details}


def _rp(request_id: int, *, error: bool = False, supply_of: bool = False) -> bytes:
    return encode_object(2, struct.pack("!II", 0x80 if supply_of else 0, request_id),
                         flags=0 if error else 2)


def _error_message(request_id: int | None, error_type: int, error_value: int) -> bytes:
    prefix = _rp(request_id, error=True) if request_id is not None else b""
    return encode_message(ERROR, prefix + encode_object(13, bytes((0, 0, error_type, error_value))))


def request_error(body: bytes, error_type: int = 5, error_value: int = 0,
                  reason: str = "Path computation unavailable") -> PathReply:
    """Return bounded errors correlated only with well-formed, nonzero RP IDs."""
    if any(type(value) is not int or not 0 <= value <= 255 for value in (error_type, error_value)):
        raise ValueError("PCEP error type/value must be unsigned octets")
    ids: list[int | None] = []
    try:
        if not isinstance(body, bytes) or len(body) > 65528:
            raise PcepProtocolError("Invalid request body")
        for obj in parse_objects(body):
            if (obj.object_class, obj.object_type) != (2, 1) or len(obj.body) < 8:
                continue
            try:
                parse_tlvs(obj.body[8:])
            except PcepProtocolError:
                continue
            request_id = int.from_bytes(obj.body[4:8], "big")
            if request_id and request_id not in ids:
                ids.append(request_id)
                if len(ids) == MAX_REQUESTS:
                    break
    except (PcepProtocolError, TypeError, ValueError):
        ids = []
    if not ids:
        ids = [None]
    return PathReply(
        tuple(_error_message(value, error_type, error_value) for value in ids),
        tuple(_outcome(value, "REJECTED", reason, error_type=error_type, error_value=error_value)
              for value in ids),
    )


def _groups(body: bytes) -> tuple[tuple[PcepObject, ...], ...]:
    if not isinstance(body, bytes) or not body or len(body) > 65528:
        raise PcepProtocolError("Empty or oversized PCReq", 6, 1)
    objects = parse_objects(body)
    if len(objects) > MAX_OBJECTS:
        raise PcepProtocolError("PCReq object limit exceeded", 5, 0)
    groups: list[list[PcepObject]] = []
    for obj in objects:
        if obj.object_class == 2:
            groups.append([])
        if not groups:
            raise PcepProtocolError("PCReq must start with RP; SVEC is unsupported", 6, 1)
        groups[-1].append(obj)
    if len(groups) > MAX_REQUESTS:
        raise PcepProtocolError("PCReq batch exceeds eight requests", 5, 0)
    ids: set[int] = set()
    for group in groups:
        rp = group[0]
        if rp.object_type != 1 or len(rp.body) < 8:
            raise PcepProtocolError("Malformed RP object", 4, 2)
        parse_tlvs(rp.body[8:])
        request_id = int.from_bytes(rp.body[4:8], "big")
        if not request_id or request_id in ids:
            raise PcepProtocolError("PCReq IDs must be nonzero and unique within the batch", 8, 0)
        ids.add(request_id)
    return tuple(tuple(group) for group in groups)


def _unsupported(reason: str, *, value: int | None = None) -> PcepProtocolError:
    # Unsupported classes/types have assigned 4/x errors. Other deliberately
    # narrow profile restrictions are local policy, not unsupported OFs (4/4).
    return PcepProtocolError(reason, 5 if value is None else 4, 0 if value is None else value)


def _decode(group: tuple[PcepObject, ...]) -> _Request:
    rp = group[0]
    if not rp.flags & 2:
        raise PcepProtocolError("RP requires the P flag", 10, 1)
    rp_flags, request_id = struct.unpack("!II", rp.body[:8])
    if rp_flags & 0x18:
        raise _unsupported("Reoptimization and bidirectional requests are unsupported")
    # IANA-assigned RP extensions (RG/C/F/N/E/M/D/P/VSPT) carry semantics this
    # implementation cannot honor. Unassigned reserved bits remain ignored.
    if rp_flags & 0x1FF40:
        raise _unsupported("Extended RP computation flags are unsupported")
    if parse_tlvs(rp.body[8:]):
        raise _unsupported("RP TLV constraints are unsupported")
    if len(group) < 2 or group[1].object_class != 4:
        raise PcepProtocolError("END-POINTS object missing", 6, 3)
    endpoints = group[1]
    if endpoints.object_type != 1 or len(endpoints.body) != 8:
        raise _unsupported("Only IPv4 END-POINTS are supported", value=2)
    if not endpoints.flags & 2:
        raise PcepProtocolError("END-POINTS requires the P flag", 10, 1)
    source, destination = (str(IPv4Address(endpoints.body[index:index + 4])) for index in (0, 4))
    if source == destination:
        raise _unsupported("Source and destination must differ")
    bandwidth: bytes | None = None
    lsp_body: bytes | None = None
    setup = hold = 0  # RFC 5440 LSPA-absent defaults; not ServiceDemand's defaults.
    affinity = Affinity()
    max_metric: int | None = None
    explicit_of = False
    seen: set[int] = set()
    metric_flags: set[int] = set()
    for obj in group[2:]:
        kind = obj.object_class
        if kind not in {5, 6, 9, 21, 32}:
            raise _unsupported(f"Unsupported request object class {kind}", value=1)
        if obj.object_type != 1:
            raise _unsupported(f"Unsupported request object type {kind}/{obj.object_type}", value=2)
        if kind != 6 and kind in seen:
            raise _unsupported(f"Duplicate request object class {kind}")
        seen.add(kind)
        if kind == 5:
            if len(obj.body) != 4:
                raise _unsupported("BANDWIDTH must contain one float32")
            amount = struct.unpack("!f", obj.body)[0]
            if not math.isfinite(amount) or amount <= 0:
                raise _unsupported("Requested bandwidth must be finite and positive")
            bandwidth = obj.body
        elif kind == 9:
            if len(obj.body) < 16:
                raise _unsupported("LSPA body is truncated")
            if parse_tlvs(obj.body[16:]):
                raise _unsupported("LSPA TLV constraints are unsupported")
            exclude, include_any, include_all, setup, hold, flags, _ = struct.unpack("!IIIBBBB", obj.body[:16])
            if setup > 7 or hold > 7 or setup < hold:
                raise _unsupported("LSPA requires priorities 0..7 and setup >= holding")
            if flags & 3:
                raise _unsupported("Local protection and protection enforcement cannot be established from this TED")
            def groups(mask: int) -> tuple[str, ...]:
                return tuple(f"bit-{bit}" for bit in range(32) if mask & (1 << bit))
            affinity = Affinity(groups(exclude), groups(include_any), groups(include_all))
        elif kind == 6:
            if len(obj.body) != 8:
                raise _unsupported("METRIC body must contain eight bytes")
            flags, metric_type = obj.body[2:4]
            if metric_type != 2:
                raise _unsupported("Only TE METRIC type 2 is supported")
            amount = struct.unpack("!f", obj.body[4:])[0]
            if not math.isfinite(amount) or amount < 0:
                raise _unsupported("METRIC must be finite and nonnegative")
            bound = flags & 1
            if bound in metric_flags:
                continue  # RFC 5440: only the first same-type/same-B metric applies.
            metric_flags.add(bound)
            if bound:
                max_metric = math.floor(amount)
        elif kind == 21:
            if len(obj.body) < 4:
                raise _unsupported("OF body is truncated")
            if parse_tlvs(obj.body[4:]):
                raise _unsupported("OF TLV constraints are unsupported")
            if int.from_bytes(obj.body[:2], "big") != 1:
                raise _unsupported("Only minimum additive TE cost (OF code 1) is supported", value=4)
            explicit_of = True
        else:
            # Request identification is distinct from a PCRpt: TLV18 is not
            # mandatory here, and this object never grants reservation credit.
            if len(obj.body) < 4 or int.from_bytes(obj.body[:4], "big") >> 12 == 0xFFFFF:
                raise _unsupported("Invalid request LSP identifier")
            tlvs = parse_tlvs(obj.body[4:])
            identifiers = [value for kind, value in tlvs if kind == 18]
            if len(identifiers) > 1 or any(len(value) != 16 for value in identifiers):
                raise _unsupported("Invalid request IPv4 LSP identifiers")
            if any(kind == 19 for kind, _ in tlvs):
                raise _unsupported("IPv6 LSP identification is unsupported")
            if any(kind not in {17, 18} for kind, _ in tlvs):
                raise _unsupported("Unknown request LSP TLVs are unsupported")
            lsp_body = (int.from_bytes(obj.body[:4], "big") & 0xFFFFF000).to_bytes(4, "big") + obj.body[4:]
    if bandwidth is None:
        raise _unsupported("Positive BANDWIDTH type 1 is required by this computation profile")
    amount = struct.unpack("!f", bandwidth)[0]
    return _Request(request_id, source, destination, math.ceil(amount * 8), bandwidth,
                    setup, hold, affinity, max_metric, bool(rp_flags & 0x80), explicit_of, lsp_body)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError(f"Nonfinite JSON constant: {value}")


def _read_regular(path: Path) -> bytes:
    if not path.is_file():
        raise BgplsError(f"Required collector file unavailable: {path.name}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise BgplsError("Collector input is not a bounded regular file")
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise BgplsError("Collector input exceeds 1 MiB")
    return raw


def _mapping(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    if not isinstance(value, dict):
        raise BgplsError("Collector JSON must contain an object")
    return value


class PcepPathComputer:
    """Compute independent unidirectional IPv4 paths; no admission guarantees."""

    def __init__(self, *, raw_path: Path, status_path: Path, topology_path: Path,
                 required_peers: tuple[str, ...], node_names: dict[str, str], headends: dict[str, str],
                 max_age_seconds: float = 30, ospf_multi_area: OspfMultiAreaPolicy | None = None,
                 domain_policy: DomainDelegationPolicy | None = None):
        if isinstance(max_age_seconds, bool) or not math.isfinite(max_age_seconds) or not 0 < max_age_seconds <= 30:
            raise ValueError("Computation freshness must be positive and at most 30 seconds")
        if not required_peers or len(set(required_peers)) != len(required_peers):
            raise ValueError("Unique required BGP-LS peers are required")
        self.required_peers = tuple(str(IPv4Address(peer)) for peer in required_peers)
        self.node_names = {str(IPv4Address(address)): validate_identifier(name) for address, name in node_names.items()}
        if ((not self.node_names and domain_policy is None) or
                len(set(self.node_names.values())) != len(self.node_names)):
            raise ValueError("Node mapping must be nonempty and one-to-one")
        self.headends = {str(IPv4Address(peer)): str(IPv4Address(source)) for peer, source in headends.items()}
        if ((not self.headends and domain_policy is None) or
                any(source not in self.node_names for source in self.headends.values())):
            raise ValueError("Headends must map allowed PCCs to known source loopbacks")
        self.raw_path, self.status_path, self.topology_path = Path(raw_path), Path(status_path), Path(topology_path)
        if len({path.resolve() for path in (self.raw_path, self.status_path, self.topology_path)}) != 3:
            raise ValueError("Collector raw, status and topology paths must be distinct")
        self.max_age_seconds = max_age_seconds
        if ospf_multi_area is not None:
            if not isinstance(ospf_multi_area, OspfMultiAreaPolicy):
                raise ValueError("OSPF multi-area computation requires a validated identity policy")
            ospf_multi_area = parse_ospf_multi_area_policy(ospf_multi_area.document())
            for binding in ospf_multi_area.bindings:
                name = self.node_names.get(binding.te_router_id)
                if name != binding.node_id and (name is not None or domain_policy is None):
                    raise ValueError("OSPF multi-area bindings must match the computation node map")
        self.ospf_multi_area = ospf_multi_area
        self.domain_policy = domain_policy

    def source_for_peer(self, peer: str, topology: Topology | None = None) -> str | None:
        """Resolve a static binding or a unique member of the trusted live domain.

        Discovery never changes the static mappings bound to managed recovery.
        The domain transport profile requires the PCC to use its TE loopback.
        """
        if peer in self.headends:
            return self.headends[peer]
        if self.domain_policy is None:
            return None
        if topology is None:
            topology, _ = self.validated_snapshot()
        return self.domain_policy.source(peer, topology)

    def validated_peer(self, peer: str) -> bool:
        if self.domain_policy is None:
            return False
        topology, _ = self.validated_snapshot()
        return self.domain_policy.source(peer, topology) is not None

    def _snapshot(self) -> tuple[Topology, str]:
        paths = (self.raw_path, self.status_path, self.topology_path)
        before = tuple(_read_regular(path) for path in paths)
        capture, status, feed = (_mapping(raw) for raw in before)
        checksum = status.pop("checksum", None)
        if type(status.get("version")) is not int or status["version"] != SCHEMA_VERSION or checksum != digest(status):
            raise BgplsError("Collector status checksum/schema is invalid")
        now = _utcnow()
        observed = status.get("observed_at")
        try:
            observed_number = (float(observed) if isinstance(observed, (float, int))
                               and not isinstance(observed, bool) else math.nan)
        except OverflowError:
            observed_number = math.nan
        if (not math.isfinite(observed_number) or
                not -1 <= now.timestamp() - observed_number <= self.max_age_seconds or status.get("status") != "ok"):
            raise BgplsError("Collector status is unavailable, expired or from the future")
        timestamp = capture.get("captured_at")
        metadata = status.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("timestamp") != timestamp:
            raise BgplsError("Collector status does not identify this raw capture")
        if not isinstance(feed.get("metadata"), dict) or feed["metadata"].get("timestamp") != timestamp:
            raise BgplsError("Collector feed does not identify this raw capture")
        topology = parse_gobgp_snapshot(capture, required_peers=self.required_peers,
                                        node_names=self.node_names, now=now, max_age_seconds=self.max_age_seconds,
                                        ospf_multi_area=self.ospf_multi_area)
        for item in (metadata, feed["metadata"]):
            if (item.get("source") != "bgp-ls" or item.get("session_state") != "ESTABLISHED" or
                    item.get("required_peers") != sorted(self.required_peers)):
                raise BgplsError("Collector provenance contradicts configured BGP-LS peers")
            if self.ospf_multi_area is None:
                if "ospf_multi_area" in item or "ospf_multi_area_digest" in item:
                    raise BgplsError("Collector OSPF area policy differs from computation policy")
            elif (item.get("ospf_multi_area") != self.ospf_multi_area.document() or
                  item.get("ospf_multi_area_digest") != self.ospf_multi_area.digest):
                raise BgplsError("Collector OSPF area policy differs from computation policy")
        if status.get("nodes") != len(topology.nodes) or status.get("directed_links") != len(topology.links):
            raise BgplsError("Collector status contradicts the raw graph size")
        if not topology.nodes or not topology.links or len(topology.nodes) > MAX_NODES or len(topology.links) > MAX_EDGES:
            raise BgplsError("BGP-LS graph is empty or exceeds computation bounds")
        if any(item.get("nlri_type") not in {3, 4} for item in topology.raw_metadata["excluded_objects"]):
            raise BgplsError("BGP-LS contains incomplete or unsupported TE objects")
        if before != tuple(_read_regular(path) for path in paths):
            raise BgplsError("Collector changed during topology validation; retry")
        evidence = before[0]
        if self.ospf_multi_area is not None:
            evidence += b"\0ospf-multi-area:" + self.ospf_multi_area.digest.encode("ascii")
        return topology, hashlib.sha256(evidence).hexdigest()

    def validate_reply(self, reply: PathReply) -> bool:
        """Revalidate collector health/freshness and exact raw bytes before send."""
        if reply.topology_digest is None:
            return bool(reply.messages) and all(
                isinstance(message, bytes) and len(message) >= 8 and message[1] == ERROR and
                int.from_bytes(message[2:4], "big") == len(message) for message in reply.messages
            )
        try:
            _, current_digest = self._snapshot()
            return current_digest == reply.topology_digest
        except (ValueError, OSError, RuntimeError, RecursionError):
            return False

    def validated_snapshot(self) -> tuple[Topology, str]:
        """Return the same fresh, internally consistent graph used for computation.

        This is observation evidence, not per-LSP ownership or admission proof.
        """
        return self._snapshot()

    def __call__(self, peer: str, body: bytes) -> PathReply:
        try:
            groups = _groups(body)
        except PcepProtocolError as exc:
            return request_error(body, exc.error_type, exc.error_value, str(exc))
        topology: Topology | None = None
        topology_digest: str | None = None
        topology_error: str | None = None
        try:
            topology, topology_digest = self._snapshot()
            source = self.source_for_peer(peer, topology)
        except (ValueError, OSError, RuntimeError, RecursionError) as exc:
            source = self.headends.get(peer)
            topology_error = str(exc)
        if source is None:
            return request_error(body, reason="PCC is not a current member of the managed topology domain")
        domain_member = bool(self.domain_policy is not None and topology is not None and
                             self.domain_policy.source(peer, topology) is not None)
        if self.domain_policy is not None and topology is not None and not domain_member:
            return request_error(body, reason="PCC is not a current member of the managed topology domain")
        requests: list[_Request | PcepProtocolError] = []
        for group in groups:
            try:
                decoded = _decode(group)
                if decoded.source != source:
                    raise PcepProtocolError("PCC is not authorized for the requested source", 5, 0)
                if self.domain_policy is not None and domain_member:
                    if (decoded.bandwidth_bps > self.domain_policy.max_bandwidth_bps or topology is None or
                            not self.domain_policy.destination(decoded.destination, topology)):
                        raise PcepProtocolError("Request exceeds the managed domain policy", 5, 0)
                requests.append(decoded)
            except PcepProtocolError as exc:
                requests.append(exc)
        messages: list[bytes] = []
        outcomes: list[dict[str, Any]] = []
        for group, request in zip(groups, requests):
            request_id = int.from_bytes(group[0].body[4:8], "big")
            if isinstance(request, PcepProtocolError):
                messages.append(_error_message(request_id, request.error_type, request.error_value))
                outcomes.append(_outcome(request_id, "REJECTED", str(request),
                                         error_type=request.error_type, error_value=request.error_value))
                continue
            if topology is None:
                messages.append(_error_message(request_id, 5, 0))
                outcomes.append(_outcome(request_id, "TOPOLOGY_UNAVAILABLE", topology_error or "No fresh TED"))
                continue
            try:
                graph = topology
                if self.domain_policy is not None and domain_member:
                    graph = self.domain_policy.graph(topology)
                message, outcome = self._compute(graph, request)
                messages.append(message)
                outcomes.append(outcome)
            except CspfNoPath as exc:
                messages.append(encode_message(PCREP, _rp(request_id) + encode_object(3, bytes(4))))
                outcomes.append(_outcome(request_id, "NO_PATH", str(exc)))
            except CspfSearchLimit as exc:
                messages.append(_error_message(request_id, 5, 0))
                outcomes.append(_outcome(request_id, "SEARCH_LIMIT", str(exc)))
            except (ValueError, RuntimeError) as exc:
                messages.append(_error_message(request_id, 5, 0))
                outcomes.append(_outcome(request_id, "REJECTED", str(exc)))
        return PathReply(tuple(messages), tuple(outcomes), topology_digest)

    def _compute(self, topology: Topology, request: _Request) -> tuple[bytes, dict[str, Any]]:
        nodes = {node.loopback: node.id for node in topology.nodes.values()}
        if len(nodes) != len(topology.nodes) or request.source not in nodes or request.destination not in nodes:
            raise ValueError("Endpoints do not identify unique nodes in the current TED")
        demand = ServiceDemand(
            name=f"pcep-{request.request_id}", source=nodes[request.source], destination=nodes[request.destination],
            bandwidth_mbps=request.bandwidth_bps / 1_000_000, reservation_bw_bps=request.bandwidth_bps,
            setup_priority=request.setup_priority, hold_priority=request.hold_priority, affinity=request.affinity,
            constraints=PathConstraints(max_metric=request.max_metric, max_candidates=1, search_budget=SEARCH_BUDGET),
        )
        result = compute_cspf(topology, demand)
        addresses = []
        for hop in result.explicit_hops:
            if hop.address is None:
                raise ValueError("Selected path has no numbered strict ERO")
            addresses.append(IPv4Address(hop.address))
        ero = b"".join(bytes((1, 8)) + address.packed + bytes((32, 0)) for address in addresses)
        response = _rp(request.request_id, supply_of=request.supply_of)
        if request.lsp_body is not None:
            response += encode_object(32, request.lsp_body)
        if request.supply_of or request.explicit_of:
            response += encode_object(21, struct.pack("!HH", 1, 0))
        response += encode_object(7, ero)
        response += encode_object(5, request.bandwidth_wire)
        response += encode_object(6, bytes((0, 0, 0, 2)) + struct.pack("!f", result.total_metric))
        return encode_message(PCREP, response), _outcome(
            request.request_id, "PATH", "Minimum TE-cost path computed; RSVP admission remains with the PCC",
            source=request.source, destination=request.destination, bandwidth_bps=request.bandwidth_bps,
            setup_priority=request.setup_priority, hold_priority=request.hold_priority,
            path_nodes=list(result.path_nodes), ero=[str(address) for address in addresses],
            path_link_ids=[link.id for link in result.path_links],
            total_metric=result.total_metric, topology_timestamp=topology.raw_metadata["timestamp"],
        )
