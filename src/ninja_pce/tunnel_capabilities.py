"""Informational tunnel eligibility; active admission remains in its backend.

The existing qualified IOS XE lab workflow is distinct from production control.
Neither a synchronized PCC nor local locks establish cross-controller fencing.
This module performs no IO and grants no authority.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

GLOBAL_CHECKS = (
    "authenticated_operator", "provisioning_service_ready", "api_qualified", "backend_qualified",
    "backend_supported", "writes_enabled", "topology_ready", "pcep_listener_ready", "pcep_authorized",
    "recovery_clear", "topology_policy_matches",
)
HEADEND_CHECKS = ("domain_eligible", "transport_enrolled", "transport_ready", "synchronized_headend")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def check(state: str, code: str, message: str, next_action: str, *, required: bool = True) -> dict[str, Any]:
    return {"state": state, "code": code, "message": message, "next_action": next_action, "required": required}


def _normalize(value: Any, *, required: bool = True) -> dict[str, Any]:
    unknown = check("unknown", "CAPABILITY_EVIDENCE_UNKNOWN", "Current prerequisite evidence is unavailable.",
                    "Refresh the capability report and inspect the qualified service locally.", required=required)
    if not isinstance(value, dict):
        return unknown
    state, code = value.get("state"), value.get("code")
    if (not isinstance(state, str) or state not in {"pass", "fail", "unknown"} or
            not isinstance(code, str) or not _CODE.fullmatch(code) or
            not isinstance(value.get("message"), str) or not isinstance(value.get("next_action"), str)):
        return unknown
    return check(state, code, value["message"][:512], value["next_action"][:768], required=required)


def _checks(values: Any, mandatory: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    values = values if isinstance(values, dict) else {}
    result = {key: _normalize(values.get(key)) for key in mandatory}
    for key, value in list(values.items())[:32]:
        if isinstance(key, str) and _KEY.fullmatch(key) and key not in result and len(result) < 24:
            result[key] = _normalize(value)
    if len(values) > 24:
        result["check_limit"] = check("unknown", "CAPABILITY_CHECK_LIMIT",
            "Prerequisite coverage exceeds the bounded report profile.",
            "Review a smaller qualified prerequisite scope before requesting new work.")
    return result


def _passed(checks: dict[str, dict[str, Any]]) -> bool:
    return bool(checks) and all(item["state"] == "pass" for item in checks.values() if item["required"])


def capability_report(checks: dict[str, Any], headends: dict[str, Any], *, scope: str,
                      enabled: bool = False, node_enabled: dict[str, bool] | None = None) -> dict[str, Any]:
    """Build a bounded report from one backend observation or observer snapshot."""
    global_checks = _checks(checks, GLOBAL_CHECKS)
    global_checks["exclusive_controller_ownership"] = {
        **check("unknown", "CONTROL_OWNERSHIP_UNVERIFIED",
                "Exclusive ownership across controllers and PCC fencing have not been established.",
                "Keep production control disabled until independent scope-bound ownership and PCC fencing are enforced.",
                required=False), "blocking_scopes": ["production"],
    }
    if len(headends) > 256:
        global_checks["endpoint_limit"] = check("fail", "CAPABILITY_ENDPOINT_LIMIT",
            "The capability endpoint limit was exceeded.", "Review the bounded domain scope before requesting new work.")
    qualified_scope = scope == "existing-qualified-profile"
    global_ready = qualified_scope and enabled is True and _passed(global_checks)
    nodes: dict[str, dict[str, Any]] = {}
    for name, value in list(headends.items())[:256]:
        if not isinstance(name, str) or not name or len(name) > 128:
            continue
        node_checks = _checks(value.get("checks") if isinstance(value, dict) else None, HEADEND_CHECKS)
        nodes[name] = {"available": global_ready and _passed(node_checks) and
                       (node_enabled is None or node_enabled.get(name) is True), "checks": node_checks}
    available = global_ready and any(node["available"] for node in nodes.values())
    all_checks = [*global_checks.values(), *(item for node in nodes.values() for item in node["checks"].values())]
    return {
        "schema_version": 1, "capability": "tunnel_creation", "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": scope, "available": available, "checks": global_checks, "headends": nodes,
        "next_action": ("Select an eligible headend, review a fresh exact plan, and explicitly approve delivery."
                        if available else "Review the failed or unknown prerequisites before creating a new tunnel."),
        "informational": True, "submission_revalidated": True, "complete": all(item["state"] != "unknown" for item in all_checks),
        "production_available": False, "control_ownership_verified": False,
        "control_authority_verified": False, "forwarding_verified": False,
        "limitations": [
            "Availability concerns the existing independently qualified IOS XE lab profile only.",
            "Unknown cross-controller ownership blocks production eligibility; local locks and PCC synchronization are insufficient.",
            "The backend rechecks current policy, exact approval, ownership, transport and capacity before delivery.",
            "RSVP/PCEP verification and traffic forwarding are separate; this report verifies neither.",
        ],
    }


def observer_options(snapshot: dict[str, Any], *, authenticated: bool = False) -> dict[str, Any]:
    """Explain observation surfaces without instantiating a provisioning backend."""
    mode = snapshot.get("operating_mode")
    scope = "demo" if snapshot.get("mode") == "demo" else "observe" if mode == "observe" else "standalone"
    checks = {
        "authenticated_operator": check("pass" if authenticated else "unknown", "ADMIN_SESSION_AUTHENTICATED" if authenticated else "PROVISIONING_OPERATOR_UNCONFIRMED",
            "This administration session is authenticated; separate provisioning authority is still required." if authenticated else
            "This endpoint does not establish an authenticated provisioning operator.",
            "Use the existing authenticated provisioning deployment for its own operator session."),
        "provisioning_service_ready": check("fail", "PROVISIONING_SERVICE_NOT_CONNECTED",
            "This observation endpoint has no connected qualified provisioning backend.",
            "Use the separately qualified HTTPS provisioning deployment; setup cannot enable router writes."),
        "api_qualified": check("fail", "PROVISIONING_API_NOT_QUALIFIED",
            "This API surface is read-only.", "Keep this observer isolated from active PCEP ownership."),
        "backend_qualified": check("unknown", "PROVISIONING_BACKEND_QUALIFICATION_UNKNOWN",
            "No qualified write backend is selected in this endpoint.", "Qualify a separate provisioning deployment before requesting delivery."),
        "backend_supported": check("fail", "PROVISIONING_BACKEND_NOT_CONNECTED",
            "No exact-plan IOS XE provisioning backend is connected.", "Follow the existing configuration provisioning deployment guide."),
        "writes_enabled": check("fail", "OBSERVER_WRITES_DISABLED", "This observation surface grants no network write authority.",
            "Continue observation here; enabling writes requires a separate qualified deployment."),
        "pcep_authorized": check("fail", "PCEP_WRITE_AUTHORITY_UNAVAILABLE",
            "Observed PCC capabilities or delegation do not authorize this endpoint to write.",
            "Review independent control policy, immutable ownership and PCC fencing before active control."),
    }
    sources = snapshot.get("sources")
    sources = {item.get("id"): item for item in sources[:16] if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(sources, list) else {}
    topology_valid = (scope != "demo" and snapshot.get("topology_complete") is True and
                      sources.get("topology", {}).get("state") == "fresh" and sources.get("bgpls", {}).get("state") == "fresh")
    checks["topology_ready"] = check("pass" if topology_valid else "unknown", "TOPOLOGY_READY" if topology_valid else "TOPOLOGY_READINESS_UNCONFIRMED",
        "A fresh validated Node and Link observation is available." if topology_valid else "A current validated topology is not established.",
        "Use diagnostics to inspect BGP-LS freshness, synchronization and domain identities.")
    capability = capability_report(checks, {}, scope=scope)
    return {"enabled": False, "headends": [], "destinations": [], "headend_readiness": {}, "max_bandwidth_bps": 0,
            "requires_plan": True, "backend": "unavailable", "capability": capability,
            "reason": "Tunnel creation is disabled in the demo workspace." if scope == "demo" else
                      "Observation setup does not enable tunnel provisioning." if authenticated else
                      "This read-only dashboard has no qualified provisioning backend connected.",
            "policy": {"read_only": True, "forwarding_verification": "unverified"}}
