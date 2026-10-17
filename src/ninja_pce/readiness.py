"""Mode-aware readiness derived from validated observations and runtime records."""
from __future__ import annotations

from typing import Any


def evaluate_readiness(snapshot: dict[str, Any], operating_mode: str,
                       processes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Readiness never grants network authority or certifies forwarding."""
    sources = {item["id"]: item for item in snapshot.get("sources", [])}
    runtime = {item["component"]: item for item in processes or []}
    scope = "runtime" if processes is not None else "observations"

    def finding(state: str, message: str, *, ready: bool = False) -> dict[str, Any]:
        return {"state": state, "ready": ready, "message": message, "scope": scope,
                "operating_mode": operating_mode, "control_authority_verified": False,
                "forwarding_verified": False}

    if operating_mode not in {"observe", "pcep-control"}:
        return finding("CONFIGURATION_MISMATCH", "The configured operating mode is unsupported.")
    if any(item.get("state") == "config_mismatch" for item in runtime.values()):
        return finding("CONFIGURATION_MISMATCH", "Runtime records do not match this configuration; review and restart.")
    if "dashboard" in runtime and runtime["dashboard"].get("state") != "running":
        return finding("MANAGEMENT_UNAVAILABLE", "The configured dashboard is stopped or unobserved.")
    if "bgpls" in runtime and runtime["bgpls"].get("state") != "running":
        return finding("BGPLS_UNAVAILABLE", "The topology collector is stopped or failed; inspect its recorded fault.")
    topology = sources.get("topology", {})
    bgpls = sources.get("bgpls", {})
    if topology.get("state") == "invalid" or bgpls.get("state") == "invalid":
        return finding("TOPOLOGY_INCOMPLETE", "BGP-LS evidence, identities or snapshot consistency did not validate.")
    if topology.get("state") == "stale" or bgpls.get("state") == "stale":
        return finding("TOPOLOGY_STALE", "The last valid topology is stale; restore the BGP-LS source and collection.")
    if bgpls.get("state") != "fresh" or topology.get("state") != "fresh":
        return finding("WAITING_FOR_BGPLS", "Waiting for a valid BGP-LS session, End-of-RIB and consistent topology.")
    counts = snapshot.get("summary", {})
    if (snapshot.get("topology_complete") is False or
            "nodes" in counts and counts["nodes"] == 0 or
            "directed_links" in counts and counts["directed_links"] == 0):
        return finding("TOPOLOGY_INCOMPLETE", "The graph is empty or contains excluded Node/Link evidence.")
    if operating_mode == "observe":
        return finding("READY_OBSERVING", "Fresh validated topology is ready for observation; PCEP is disabled.", ready=True)
    if "pcep" in runtime and runtime["pcep"].get("state") != "running":
        return finding("PCEP_UNAVAILABLE", "The PCEP component is stopped or unobserved.")
    if sources.get("pcep", {}).get("state") != "fresh":
        return finding("PCEP_UNAVAILABLE", "Current PCEP listener observations are unavailable or stale.")
    synchronized = sum(peer.get("fresh") is True and peer.get("synchronized") is True
                       for peer in snapshot.get("peers", []) if peer.get("protocol") == "PCEP")
    if not synchronized:
        return finding("WAITING_FOR_PCC", "No fresh synchronized PCC session; controller bootstrap or PCC connectivity is incomplete.")
    return finding("READY_PCEP", "Topology and PCC synchronization are ready under existing PCEP policy; ownership and forwarding remain unverified.",
                   ready=True)
