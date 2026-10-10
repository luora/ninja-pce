"""Structural boundaries shared by file-fed and live controller adapters."""
from __future__ import annotations

from typing import Any, Protocol

from .models import CspfResult, ServiceDemand, Topology


class TopologyProvider(Protocol):
    def snapshot(self) -> Topology: ...


class TrafficEngineeringDatabase(Protocol):
    def current(self) -> Topology: ...

    def replace(self, snapshot: Topology, expected_revision: str) -> str: ...


class PathComputationEngine(Protocol):
    def compute(self, topology: Topology, demand: ServiceDemand) -> CspfResult: ...


class ReservationManager(Protocol):
    def effective_topology(self, topology: Topology, lsp_id: str) -> Topology: ...


class LspStateRepository(Protocol):
    def load(self, lsp_id: str) -> dict[str, Any]: ...

    def save(self, lsp_id: str, state: dict[str, Any]) -> None: ...


class SouthboundAdapter(Protocol):
    def apply(self, plan: Any, approval: Any) -> Any: ...


class OperationalVerifier(Protocol):
    def verify(self, plan: Any, outputs: Any) -> Any: ...


class ReconciliationController(Protocol):
    def reconcile(self, demand: ServiceDemand) -> Any: ...
