"""Pure diagnostics for bounded, validated controller observations.

These findings describe recorded evidence. They never probe a socket, certify
current reachability, merge router identities, or grant control authority.
"""
from __future__ import annotations

import math
from datetime import datetime
from ipaddress import ip_address
from typing import Any

_SOURCE_STATES = {"fresh", "stale", "invalid", "unavailable", "unconfigured", "disabled"}
_DOWN_STATES = {"IDLE", "CONNECT", "ACTIVE", "OPENSENT", "OPENCONFIRM", "DISCONNECTED", "CLOSED", "DOWN"}
_STARTUP_CODES = {
    "GOBGP_PORT_IN_USE", "GOBGP_CONFIG_INVALID", "GOBGP_BINARY_INVALID", "GOBGP_CONFIG_CHANGED",
    "GOBGP_UNEXPECTED_EXIT", "PCEP_PORT_IN_USE", "DASHBOARD_PORT_IN_USE", "LISTENER_ADDRESS_UNAVAILABLE",
    "LISTENER_PERMISSION_DENIED", "COMPONENT_ALREADY_SUPERVISED", "BGPLS_CLIENT_MISSING",
    "CONTROLLER_CONFIGURATION_INVALID", "CONTROLLER_CONFIGURATION_CHANGED", "STARTUP_LOCAL_RESOURCE_ERROR",
    "STARTUP_CONFIGURATION_INVALID", "COMPONENT_PERMANENT_STARTUP_FAILURE", "BGPLS_ALREADY_COLLECTING",
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any, limit: int = 512) -> list[dict[str, Any]]:
    return [item for item in value[:limit] if isinstance(item, dict)] if isinstance(value, list) else []


def _address(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64 or "%" in value:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _source_evidence(source: dict[str, Any]) -> dict[str, Any]:
    state = source.get("state")
    evidence: dict[str, Any] = {"source_state": state if isinstance(state, str) and state in _SOURCE_STATES else "unknown"}
    age = source.get("age_seconds")
    if isinstance(age, (float, int)) and not isinstance(age, bool) and 0 <= age <= 10**12 and math.isfinite(age):
        evidence["age_seconds"] = age
    stamp = source.get("timestamp")
    if isinstance(stamp, str) and len(stamp) <= 64:
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                evidence["timestamp"] = parsed.isoformat()
        except ValueError:
            pass
    return evidence


def _finding(code: str, severity: str, category: str, message: str,
             remediation: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "category": category,
            "message": message, "evidence": evidence, "remediation": remediation}


def _peer_evidence(peers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"peer_count": len(peers), "peer_addresses": sorted({address for peer in peers
            if (address := _address(peer.get("address"))) is not None})[:16]}


def _bgpls(snapshot: dict[str, Any], sources: dict[str, dict[str, Any]],
           observations: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    source = sources.get("bgpls", {})
    evidence = _source_evidence(source)
    state = evidence["source_state"]
    if state == "fresh":
        result.append(_finding("BGPLS_OBSERVATION_VALID", "ok", "bgpls",
                               "A fresh collector observation validates against its captured topology.",
                               "Continue monitoring collection age and required peers.", **evidence))
    else:
        code = {"stale": "BGPLS_COLLECTOR_STALE", "invalid": "BGPLS_COLLECTOR_INVALID",
                "unavailable": "BGPLS_COLLECTOR_UNAVAILABLE"}.get(state, "BGPLS_COLLECTOR_UNKNOWN")
        result.append(_finding(code, "unknown" if state == "unknown" else "blocker", "bgpls",
                               "Current validated BGP-LS collector evidence is unavailable.",
                               "Review collector service status and collection time; restore valid observations.",
                               **evidence))
    peers = [peer for peer in _rows(snapshot.get("peers"), 128) if peer.get("protocol") == "BGP-LS"]
    session = observations.get("session_state")
    down = [peer for peer in peers if isinstance(peer.get("state"), str) and peer["state"] in _DOWN_STATES]
    if isinstance(session, str) and session in _DOWN_STATES or down:
        result.append(_finding("BGPLS_SESSION_DOWN", "blocker", "bgpls",
                               "The last collector observation reports a required BGP-LS session down.",
                               "Review the required router neighbor, AFI/SAFI, transport and managed GoBGP service.",
                               **_peer_evidence(down), **evidence))
    elif session != "ESTABLISHED" and not any(peer.get("state") == "ESTABLISHED" for peer in peers):
        result.append(_finding("BGPLS_SESSION_UNKNOWN", "unknown", "bgpls",
                               "Recorded evidence does not establish the required BGP-LS session state.",
                               "Collect current peer state through the configured read-only API; do not infer a down session.",
                               **evidence))
    eor = observations.get("eor_complete")
    if eor is False or any(peer.get("state") == "ESTABLISHED" and peer.get("synchronized") is False
                           for peer in peers):
        result.append(_finding("BGPLS_EOR_INCOMPLETE", "blocker", "bgpls",
                               "The last observation has not completed required BGP-LS End-of-RIB synchronization.",
                               "Wait for End-of-RIB and review enabled BGP-LS address families and restart state.",
                               **evidence))
    elif eor is not True and not (peers and all(peer.get("synchronized") is True for peer in peers)):
        result.append(_finding("BGPLS_EOR_UNKNOWN", "unknown", "bgpls",
                               "Required BGP-LS End-of-RIB evidence is unavailable.",
                               "Obtain a complete current collector observation before relying on the graph.", **evidence))
    rpc = observations.get("rpc_available")
    if rpc is False:
        result.append(_finding("BGPLS_RPC_UNAVAILABLE", "blocker", "bgpls",
                               "The collector recorded an unavailable read-only gRPC call.",
                               "Review the managed GoBGP API address, collector client and local service diagnostics.",
                               **evidence))
    elif rpc is True or state == "fresh":
        result.append(_finding("BGPLS_RPC_OBSERVED", "info", "bgpls",
                               "A successful collector RPC was recorded; current endpoint reachability is untested.",
                               "Use current collection and service evidence to investigate an endpoint fault.", **evidence))
    else:
        result.append(_finding("BGPLS_RPC_UNKNOWN", "unknown", "bgpls",
                               "The available snapshot does not establish why collection is unavailable.",
                               "Review the collector's structured fault; do not infer a gRPC failure from file age.",
                               **evidence))
    original_code = observations.get("diagnostic_code")
    causes = {
        "BGPLS_FAMILY_UNAVAILABLE": ("blocker", "Required BGP-LS address family is unavailable in the recorded peer response.",
                                     "Review BGP-LS AFI/SAFI enablement on the required exporter and managed speaker."),
        "BGPLS_PEER_MISSING": ("blocker", "A required BGP-LS peer is absent from the recorded response.",
                               "Compare required peer addresses with the managed speaker configuration and router persistence."),
        "BGPLS_PEER_IDENTITY_INVALID": ("blocker", "Recorded required-peer identities failed validation.",
                                        "Review duplicate neighbor identities and the trusted required-peer list."),
        "BGPLS_SOURCE_RESTARTING": ("blocker", "Recorded BGP-LS graceful-restart state prohibits ingestion.",
                                    "Wait for restart completion and collect a full synchronized observation."),
        "BGPLS_EMPTY_GRAPH": ("blocker", "The collector recorded no computable Node and Link graph.",
                              "Review required advertisements, exclusions and configured domain identities."),
        "BGPLS_PEER_METADATA_INVALID": ("unknown", "Recorded peer metadata does not establish required session or synchronization fields.",
                                        "Verify the qualified API contract and complete peer metadata; missing fields do not prove a down session."),
        "BGPLS_INPUT_INVALID": ("blocker", "The captured input failed strict collector validation.",
                                "Review the retained capture and domain/identity policy locally; no raw payload is included here."),
        "BGPLS_COLLECTION_FAILED": ("unknown", "Collection failed without a more specific structured protocol cause.",
                                    "Review the bounded collector log locally; do not infer a BGP, RPC or End-of-Rib fault."),
    }
    if state != "fresh" and isinstance(original_code, str) and original_code in causes:
        severity, message, remediation = causes[original_code]
        result.append(_finding(original_code, severity, "bgpls", message, remediation, **evidence))
    return result


def _topology(snapshot: dict[str, Any], sources: dict[str, dict[str, Any]],
              observations: dict[str, Any], bgpls: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    evidence = _source_evidence(sources.get("topology", {}))
    state = evidence["source_state"]
    summary = _mapping(snapshot.get("summary"))
    for key in ("nodes", "directed_links"):
        count = _count(summary.get(key))
        if count is not None:
            evidence[key] = count
    excluded = _count(bgpls.get("excluded_node_link_count")) or 0
    prefix = _count(bgpls.get("excluded_prefix_count")) or 0
    if state != "fresh":
        code = {"stale": "TOPOLOGY_STALE", "invalid": "TOPOLOGY_INVALID",
                "unavailable": "TOPOLOGY_UNAVAILABLE"}.get(state, "TOPOLOGY_UNKNOWN")
        result.append(_finding(code, "unknown" if state == "unknown" else "blocker", "topology",
                               "The graph lacks a current validated topology observation.",
                               "Restore a consistent collector snapshot with valid timestamps and domain identities.",
                               **evidence))
    elif evidence.get("nodes") == 0 or evidence.get("directed_links") == 0:
        result.append(_finding("TOPOLOGY_EMPTY", "blocker", "topology",
                               "The validated observation has an empty Node or directed Link graph.",
                               "Review required Node and Link advertisements and configured domain scope.", **evidence))
    elif snapshot.get("topology_complete") is False and not excluded:
        result.append(_finding("TOPOLOGY_INCOMPLETE", "blocker", "topology",
                               "The validated snapshot does not establish a complete Node and Link graph.",
                               "Review domain identities and Node/Link exclusions before using this graph.", **evidence))
    elif snapshot.get("topology_complete") is True and not excluded:
        result.append(_finding("TOPOLOGY_OBSERVATION_VALID", "ok", "topology",
                               "The snapshot contains a fresh validated Node and Link graph.",
                               "Continue monitoring freshness; this observation does not verify forwarding.", **evidence))
    if excluded:
        result.append(_finding("TOPOLOGY_NODE_LINK_EXCLUDED", "blocker", "topology",
                               "Required Node or Link advertisements were excluded from normalization.",
                               "Review excluded advertisement kinds and identity policy; retain the original capture for review.",
                               excluded_count=excluded, **evidence))
    if prefix:
        result.append(_finding("TOPOLOGY_PREFIX_EXCLUDED", "info", "topology",
                               "Optional Prefix advertisements were excluded; this alone does not invalidate the Node/Link graph.",
                               "Review optional Prefix parsing separately from Node/Link and ABR identity validation.",
                               excluded_count=prefix, **evidence))
    validated = observations.get("ospf_multi_area_policy_validated")
    if validated is True:
        result.append(_finding("ABR_PROJECTION_POLICY_VALIDATED", "ok", "topology",
                               "The recorded ABR projection matches the configured validated multi-area policy.",
                               "Review advertised area identities independently from physical router labels.", **evidence))
    elif validated is False and observations.get("abr_projection_configured") is True:
        result.append(_finding("ABR_PROJECTION_POLICY_INVALID", "blocker", "topology",
                               "The configured ABR projection was not validated against this observation.",
                               "Review each explicit area identity binding and policy digest; do not merge identities by label.",
                               **evidence))
    elif observations.get("abr_projection_configured") is False:
        result.append(_finding("ABR_PROJECTION_NOT_CONFIGURED", "info", "topology",
                               "No multi-area ABR projection is configured; existing single-area validation applies.",
                               "Review explicit ABR identity bindings before enabling multi-area projection.", **evidence))
    else:
        result.append(_finding("ABR_PROJECTION_POLICY_UNKNOWN", "unknown", "topology",
                               "No validated ABR projection policy is established by this snapshot.",
                               "If multi-area projection is required, configure and validate explicit ABR identity bindings.",
                               **evidence))
    return result


def _pcep(snapshot: dict[str, Any], sources: dict[str, dict[str, Any]],
          observations: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    evidence = _source_evidence(sources.get("pcep", {}))
    state = evidence["source_state"]
    if state != "fresh":
        code = {"stale": "PCEP_OBSERVATION_STALE", "invalid": "PCEP_OBSERVATION_INVALID",
                "unavailable": "PCEP_OBSERVATION_UNAVAILABLE"}.get(state, "PCEP_OBSERVATION_UNKNOWN")
        result.append(_finding(code, "unknown" if state == "unknown" else "blocker", "pcep",
                               "Current validated PCEP listener observations are unavailable.",
                               "Review the PCEP component and capture health; this finding does not test TCP reachability.",
                               **evidence))
    peers = _rows(observations.get("peers"), 64) or [peer for peer in _rows(snapshot.get("peers"), 128)
                                                  if peer.get("protocol") == "PCEP"]
    if not peers:
        result.append(_finding("PCEP_WAITING_FOR_PCC", "warning", "pcep",
                               "No PCC session is recorded in this observation.",
                               "Review the PCC target address and allowlist, then wait for session synchronization.",
                               **evidence))
        return result
    dead = [peer for peer in peers if peer.get("stale_reason") == "deadtimer_expired"]
    stale = [peer for peer in peers if peer.get("fresh") is not True and peer not in dead]
    syncing = [peer for peer in peers if peer.get("fresh") is True and peer.get("synchronized") is not True]
    synchronized = [peer for peer in peers if state == "fresh" and peer.get("fresh") is True and peer.get("synchronized") is True]
    groups = [
        (dead, "PCEP_DEADTIMER_EXPIRED", "blocker", "A recorded PCC session expired its PCEP deadtimer.",
         "Review PCC keepalives, transport and timer compatibility before reconnecting."),
        (stale, "PCEP_SESSION_STALE", "warning", "Recorded PCC session evidence is no longer fresh.",
         "Review current listener and PCC evidence; retained reports do not establish a live session."),
        (syncing, "PCEP_SYNC_INCOMPLETE", "warning", "A recorded fresh PCC session has not completed synchronization.",
         "Wait for the end-of-synchronization marker and review PCC report errors."),
        (synchronized, "PCEP_SESSION_SYNCHRONIZED", "ok", "Fresh synchronized PCC session evidence is recorded.",
         "Review delegation and separate authorization before any control operation; forwarding remains unverified."),
    ]
    for selected, code, severity, message, remediation in groups:
        if selected:
            result.append(_finding(code, severity, "pcep", message, remediation,
                                   **_peer_evidence(selected), **evidence))
    remote = [peer for peer in peers if peer.get("remote_stateful") is True]
    unsupported = [peer for peer in peers if peer.get("remote_stateful") is False]
    if remote:
        result.append(_finding("PCEP_REMOTE_STATEFUL_OBSERVED", "info", "pcep",
                               "A recorded peer OPEN advertises stateful PCEP capability; this grants no authority.",
                               "Review negotiated update capability and existing authorization separately.",
                               **_peer_evidence(remote), **evidence))
    if unsupported:
        result.append(_finding("PCEP_REMOTE_STATEFUL_UNAVAILABLE", "warning", "pcep",
                               "A recorded peer OPEN does not advertise stateful PCEP capability.",
                               "Review the intended PCC feature support and router configuration.",
                               **_peer_evidence(unsupported), **evidence))
    if not remote and not unsupported:
        result.append(_finding("PCEP_REMOTE_STATEFUL_UNKNOWN", "unknown", "pcep",
                               "Peer stateful capability is not established by this snapshot.",
                               "Review the peer OPEN observation; locally advertised capability does not prove peer support.",
                               **evidence))
    update = [peer for peer in peers if peer.get("remote_lsp_update") is True]
    no_update = [peer for peer in peers if peer.get("remote_lsp_update") is False]
    result.append(_finding("PCEP_REMOTE_UPDATE_OBSERVED" if update else "PCEP_REMOTE_UPDATE_UNAVAILABLE" if no_update else "PCEP_REMOTE_UPDATE_UNKNOWN",
                           "info" if update else "warning" if no_update else "unknown", "pcep",
                           "A recorded peer OPEN advertises LSP update capability; authorization remains separate." if update else
                           "The available peer OPEN evidence does not establish LSP update support for a control operation.",
                           "Review remote OPEN capability and existing policy independently before considering control.",
                           **_peer_evidence(update or no_update), **evidence))
    local = []
    for peer in peers:
        capabilities = peer.get("capabilities")
        if peer.get("local_stateful") is True or isinstance(capabilities, list) and "Stateful" in capabilities:
            local.append(peer)
    if local:
        result.append(_finding("PCEP_LOCAL_STATEFUL_ADVERTISED", "info", "pcep",
                               "The listener recorded locally advertised stateful capability.",
                               "Review peer capability separately; local advertisement grants no control authority.",
                               **_peer_evidence(local), **evidence))
    delegated = [lsp for lsp in _rows(snapshot.get("lsps"))
                 if lsp.get("fresh") is True and lsp.get("delegated") is True]
    result.append(_finding("PCEP_DELEGATION_OBSERVED" if delegated else "PCEP_DELEGATION_UNOBSERVED", "info", "pcep",
                           "Fresh delegated LSP reports are recorded; separate authorization remains required."
                           if delegated else "No fresh delegated LSP report is established by this snapshot.",
                           "Review explicit ownership and authorization policy before any control operation.",
                           delegated_lsp_count=len(delegated), **evidence))
    return result


def _runtime(processes: list[dict[str, Any]], operating_mode: str) -> list[dict[str, Any]]:
    result = []
    for process in processes[:16]:
        if not isinstance(process, dict):
            continue
        component = process.get("component")
        if not isinstance(component, str) or component not in {"bgpls", "pcep", "dashboard"} or component == "pcep" and operating_mode == "observe":
            continue
        state = process.get("state")
        state = state if isinstance(state, str) else "unknown"
        code, severity, message = {
            "running": ("RUNTIME_COMPONENT_RUNNING", "ok", "The component has a live matching runtime ownership record."),
            "config_mismatch": ("RUNTIME_CONFIGURATION_MISMATCH", "blocker", "The runtime ownership record differs from the selected configuration."),
            "failed": ("RUNTIME_COMPONENT_FAILED", "blocker", "The component recorded a runtime or startup failure."),
            "stopped": ("RUNTIME_COMPONENT_STOPPED", "warning", "The component has no live process under its recorded ownership."),
        }.get(state, ("RUNTIME_COMPONENT_UNOBSERVED", "unknown", "Matching component ownership has not been observed."))
        evidence: dict[str, Any] = {"component": component}
        status = process.get("returncode")
        if type(status) is int and -255 <= status <= 255:
            evidence["returncode"] = status
        result.append(_finding(code, severity, "runtime", message,
                               "Review the managed component record and selected configuration; reconcile ownership manually.",
                               **evidence))
        original = _mapping(process.get("finding"))
        original_code = original.get("code")
        if state != "failed" or not isinstance(original_code, str) or original_code not in _STARTUP_CODES:
            continue
        address = _address(original.get("address"))
        if address is not None:
            evidence["address"] = address
        port = original.get("port")
        if type(port) is int and 0 <= port <= 65535:
            evidence["port"] = port
        error = original.get("errno")
        if type(error) is int and 0 <= error <= 4095:
            evidence["errno"] = error
        owners = []
        for owner in _rows(original.get("owners"), 16):
            pid = owner.get("pid")
            if type(pid) is int and 0 < pid <= 2**31 - 1:
                owners.append({"pid": pid})
        if owners:
            evidence["owners"] = owners
        result.append(_finding(original_code, "blocker", "runtime",
                               "The component retained this original managed-startup finding.",
                               "Review the recorded cause and configuration. Reconcile an identified owner through its managed service; never kill an arbitrary PID automatically.",
                               **evidence))
    return result


def protocol_findings(snapshot: dict[str, Any], operating_mode: str, *,
                      processes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Describe explicit known fields without IO or mutation of the snapshot.

    ``diagnostic_observations`` is a safe projection supplied by DashboardStore.
    Missing observations remain unknown; generic collector failure cannot prove
    a BGP, RPC, EOR, identity-policy, or remote TCP failure.
    """
    sources = {str(source.get("id")): source for source in _rows(snapshot.get("sources"), 16)
               if isinstance(source.get("id"), str) and source.get("id") in {"bgpls", "topology", "pcep"}}
    observations = _mapping(snapshot.get("diagnostic_observations"))
    bgpls = _mapping(observations.get("bgpls"))
    result = _bgpls(snapshot, sources, bgpls)
    result.extend(_topology(snapshot, sources, _mapping(observations.get("topology")), bgpls))
    if operating_mode == "observe":
        result.append(_finding("PCEP_DISABLED", "info", "pcep",
                               "PCEP is disabled in observe mode; no listener or PCC session is required.",
                               "Continue read-only topology observation; enabling PCEP requires separate configuration."))
    else:
        result.extend(_pcep(snapshot, sources, _mapping(observations.get("pcep"))))
    if processes is not None:
        result.extend(_runtime(processes, operating_mode))
    return result
