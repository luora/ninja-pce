"""Permanent startup faults with structured, bounded operator guidance."""
from __future__ import annotations

import errno
import ipaddress
import socket
from typing import Any

PERMANENT_EXIT = 78
_PERMANENT_ERRNOS = {
    errno.EADDRINUSE, errno.EADDRNOTAVAIL, errno.EACCES, errno.EPERM,
    errno.ENOENT, errno.ENOTDIR, errno.ENOEXEC,
}


class PermanentStartupError(ValueError):
    """A retry cannot repair this configuration, dependency or listener fault."""

    def __init__(self, finding: dict[str, Any]):
        self.finding = dict(finding)
        super().__init__(str(finding.get("message", "Permanent startup failure")))


def startup_finding(code: str, component: str, message: str,
                    remediation: str = "Review the configuration and service logs before restarting.",
                    **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": "blocker", "component": component,
            "message": message[:1024], "remediation": remediation, **details}


def permanent_os_error(error: OSError) -> bool:
    """Classify known local startup failures; do not classify network outages."""
    return error.errno in _PERMANENT_ERRNOS


def require_available_listener(address: str, port: int, *, component: str,
                               expected_service: str | None = None) -> None:
    """Probe bind eligibility without listening, connecting or keeping a socket.

    This is a preflight, not a lock. The real listener must still handle a
    concurrent bind; no foreign process is modified or assumed replaceable.
    IPv6 is checked as a v6-only socket, matching the asyncio PCEP listener and
    GoBGP's explicit tcp6 listener.
    """
    if type(port) is not int or not 0 <= port <= 65535:
        raise PermanentStartupError(startup_finding(
            "LISTENER_CONFIGURATION_INVALID", component, "Listener port must be between 0 and 65535."))
    try:
        parsed = ipaddress.ip_address(address)
    except (ValueError, TypeError) as error:
        raise PermanentStartupError(startup_finding(
            "LISTENER_CONFIGURATION_INVALID", component, "Listener address must be a numeric IP address.",
            address=str(address)[:128], port=port)) from error
    try:
        with socket.socket(socket.AF_INET6 if parsed.version == 6 else socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if parsed.version == 6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind((str(parsed), port))
    except OSError as error:
        if not permanent_os_error(error):
            raise
        raise listener_startup_error(error, address, port, component=component,
                                     expected_service=expected_service) from error


def listener_startup_error(error: OSError, address: str, port: int, *, component: str,
                           expected_service: str | None = None) -> PermanentStartupError:
    """Attach best-effort owner evidence to the actual bind failure."""
    from .listeners import inspect_listener

    finding = inspect_listener(address, port, expected_service=expected_service)
    prefix = {"gobgpd": "GOBGP", "pcep": "PCEP", "dashboard": "DASHBOARD"}.get(component, "LISTENER")
    code = (f"{prefix}_PORT_IN_USE" if error.errno == errno.EADDRINUSE else
            "LISTENER_ADDRESS_UNAVAILABLE" if error.errno == errno.EADDRNOTAVAIL else "LISTENER_PERMISSION_DENIED")
    remediation = ("Review the detected owner and stop or reconfigure the conflicting instance through its normal "
                   "service controls before restarting. No process was stopped."
                   if error.errno == errno.EADDRINUSE else
                   "Verify that the configured address exists locally and that the service has bind permission.")
    finding.update(code=code, severity="blocker", component=component,
                   address=address, port=port, expected_service=expected_service,
                   errno=error.errno, message=f"Cannot bind {address}:{port}: {error.strerror or str(error)}",
                   remediation=remediation)
    return PermanentStartupError(finding)
