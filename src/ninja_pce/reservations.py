"""Conservative directional reservation accounting under one local directory lock."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import CspfResult, ServiceDemand, Topology
from .state import digest, load_state, write_state

RESERVATION_STATES = {
    "PLANNED", "PUSHING", "APPLIED_UNVERIFIED", "VERIFIED", "VERIFICATION_FAILED",
    "ROLLBACK_REQUIRED", "ROLLED_BACK",
}

@contextmanager
def resource_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / ".ninja-pce-resources.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def lsp_identity(demand: ServiceDemand, output_format: str) -> str:
    if output_format == "iosxr":
        tunnel = demand.output.iosxr_interface
    elif output_format == "iosxe":
        tunnel = f"Tunnel{demand.output.tunnel_id}"
    else:
        tunnel = demand.output.junos_lsp_name
    return f"{demand.source}:{output_format}:{tunnel}"


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def authoritative_snapshot(topology: Topology, max_age_seconds: float = 60.0) -> bool:
    metadata = topology.raw_metadata
    timestamp = _timestamp(metadata.get("timestamp"))
    if timestamp is None or not isinstance(metadata.get("source"), str) or not metadata["source"]:
        return False
    age = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if not (
        metadata.get("authoritative") is True
        and metadata.get("complete") is True
        and 0 <= age <= max_age_seconds
    ):
        return False
    for link in topology.directional_links:
        observed = _timestamp(link.timestamp) if link.timestamp is not None else timestamp
        source = link.provenance_source if link.provenance_source is not None else metadata["source"]
        if observed is None or source != metadata["source"] or observed > timestamp:
            return False
        link_age = (datetime.now(timezone.utc) - observed).total_seconds()
        if not 0 <= link_age <= max_age_seconds:
            return False
    return True


class ReservationLedger:
    """Caller must hold resource_lock from load through computation and persistence.

    A changed path retains all old edges. Only explicit owned observations can
    replace local pending accounting; timestamps alone never release resources.
    """

    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / ".ninja-pce-reservations.json"
        self.data = load_state(self.path)
        if not self.data:
            # Losing the ledger while keeping LSP state must not erase reservations.
            for path in state_dir.glob("*.json"):
                if path != self.path:
                    state = load_state(path, legacy=True)
                    if state.get("reservation_id"):
                        raise ValueError("Reservation ledger is missing; recover it before reconciliation")
            self.data = {"version": 2, "entries": {}}
        if not isinstance(self.data.get("entries"), dict):
            raise ValueError("Reservation ledger entries must be an object")
        for identity, entry in self.data["entries"].items():
            if not isinstance(identity, str) or not identity or not isinstance(entry, dict):
                raise ValueError("Malformed reservation identity or entry")
            if (not isinstance(entry.get("owner"), str) or not entry["owner"]
                    or entry.get("status") not in RESERVATION_STATES
                    or not isinstance(entry.get("resources"), dict)
                    or not isinstance(entry.get("current_edges"), list)):
                raise ValueError(f"Malformed reservation state for {identity}")
            bandwidth = entry.get("current_bandwidth_bps")
            if isinstance(bandwidth, bool) or not isinstance(bandwidth, int) or bandwidth <= 0:
                raise ValueError(f"Malformed reservation bandwidth for {identity}")
            if not all(isinstance(edge, str) and isinstance(amount, int) and not isinstance(amount, bool)
                       and amount > 0 for edge, amount in entry["resources"].items()):
                raise ValueError(f"Malformed directional reservation resources for {identity}")
            if not all(isinstance(edge, str) and entry["resources"].get(edge, 0) >= bandwidth
                       for edge in entry["current_edges"]):
                raise ValueError(f"Current path exceeds held resources for {identity}")

    @property
    def entries(self) -> dict[str, Any]:
        return dict(self.data["entries"])

    def assert_owner(self, identity: str, owner: str) -> None:
        entry = self.data["entries"].get(identity)
        if entry and entry["owner"] != owner:
            raise ValueError(f"Duplicate LSP identity {identity}; already owned by {entry['owner']}")
        if any(entry["owner"] == owner and key != identity for key, entry in self.entries.items()):
            raise ValueError("An existing state file cannot change LSP identity; use explicit recovery")

    def _reflected(self, identity: str, entry: dict[str, Any], topology: Topology) -> dict[str, int]:
        if not entry.get("network_attempted_at") or not authoritative_snapshot(topology):
            return {}
        metadata = topology.raw_metadata
        observations = metadata.get("reservations", [])
        if not isinstance(observations, list):
            return {}
        attempted = _timestamp(entry["network_attempted_at"])
        snapshot_at = _timestamp(metadata.get("timestamp"))
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observed = _timestamp(observation.get("observed_at"))
            edges = observation.get("edges")
            bandwidth = observation.get("bandwidth_bps")
            if (
                observation.get("lsp_id") != identity
                or observation.get("owner") != entry["owner"]
                or observation.get("source") != metadata.get("source")
                or observation.get("status") != "VERIFIED"
                or not isinstance(edges, list)
                or not all(isinstance(edge, str) for edge in edges)
                or set(edges) != set(entry["current_edges"])
                or not isinstance(bandwidth, int) or isinstance(bandwidth, bool)
                or bandwidth != entry["current_bandwidth_bps"]
                or observed is None or attempted is None or snapshot_at is None
                or not attempted <= observed <= snapshot_at
            ):
                continue
            # Confirm the TED actually debits this bandwidth; ownership alone is not sufficient.
            by_id = {link.id: link for link in topology.directional_links}
            if any(edge not in by_id or (by_id[edge].reserved_bw_bps or 0) < bandwidth for edge in edges):
                continue
            return {edge: bandwidth for edge in edges}
        return {}

    def effective_topology(self, topology: Topology, owner: str) -> Topology:
        pending: dict[str, int] = {}
        reusable: dict[str, int] = {}
        reflected_entries = {
            identity: self._reflected(identity, entry, topology)
            for identity, entry in self.entries.items()
        }
        totals: dict[str, int] = {}
        for reflected in reflected_entries.values():
            for edge, bandwidth in reflected.items():
                totals[edge] = totals.get(edge, 0) + bandwidth
        for link in topology.directional_links:
            reflected_total = totals.get(link.id, 0)
            if reflected_total > min(link.reserved_bw_bps or 0, link.bandwidth_bps - link.unreserved_bw_bps[7]):
                raise ValueError(f"Contradictory owned observations exceed TED debit on edge {link.id}")
        for identity, entry in self.entries.items():
            reflected = reflected_entries[identity]
            if entry["owner"] == owner:
                reusable = reflected
            for edge, bandwidth in entry["resources"].items():
                # Own held resources are reusable when recomputing the same LSP.
                if entry["owner"] != owner:
                    pending[edge] = pending.get(edge, 0) + max(bandwidth - reflected.get(edge, 0), 0)
        links = []
        for link in topology.directional_links:
            own = reusable.get(link.id, 0)
            links.append(replace(
                link, pending_bw_bps=link.pending_bw_bps + pending.get(link.id, 0),
                reserved_bw_bps=(link.reserved_bw_bps or 0) - own,
                reserved_bw_mbps=((link.reserved_bw_bps or 0) - own) / 1_000_000,
                unreserved_bw_bps=tuple(min(value + own, link.bandwidth_bps) for value in link.unreserved_bw_bps),
            ))
        return replace(topology, links=links)

    def reserve(self, identity: str, owner: str, result: CspfResult, plan_id: str, bandwidth_bps: int | None = None) -> None:
        self.assert_owner(identity, owner)
        timestamp = datetime.now(timezone.utc).isoformat()
        previous = self.data["entries"].get(identity, {})
        resources = dict(previous.get("resources", {}))
        bandwidth = max(result.demand.bandwidth_bps, bandwidth_bps or 0)
        for edge in result.path_links:
            resources[edge.id] = max(resources.get(edge.id, 0), bandwidth)
        self.data["entries"][identity] = {
            **previous,
            "owner": owner,
            "service": result.demand.name,
            "resources": resources,
            "current_edges": [edge.id for edge in result.path_links],
            "current_bandwidth_bps": bandwidth,
            "plan_id": plan_id,
            "created_at": previous.get("created_at", timestamp),
            "updated_at": timestamp,
            "status": "PLANNED",
        }
        self.save()

    def transition(self, identity: str, status: str) -> None:
        if status not in RESERVATION_STATES:
            raise ValueError("Unsupported reservation transition; automatic release requires explicit owned absence evidence")
        entry = self.data["entries"][identity]
        if status == "PUSHING" and entry["status"] not in {"PLANNED", "VERIFIED", "ROLLED_BACK"}:
            raise ValueError("Unresolved delivery must be observed before another write")
        entry["status"] = status
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        if status == "PUSHING":
            entry["network_attempted_at"] = entry["updated_at"]
        self.save()

    def save(self) -> None:
        self.data["revision"] = digest(self.data["entries"])
        write_state(self.path, self.data)
