"""Separate trusted topology eligibility from enrolled SSH transport identity.

Membership functions consume the caller's fresh validated TED. Transport
enrollment supplies only management connectivity and never adds or hides a
domain member. The qualified configuration backend remains Cisco IOS XE SSH.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import struct
from dataclasses import asdict
from ipaddress import ip_address
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .inventory import normalize_device_type
from .models import ManagedDevice, Topology
from .pcep_domain_policy import DomainDelegationPolicy
from .state import digest

_MAX_PIN_FILE_BYTES = 1_048_576


def domain_headends(topology: Topology, policy: DomainDelegationPolicy) -> dict[str, str]:
    """Return every uniquely identified headend in the trusted domain scope.

    Router roles and transport inventory are deliberately absent from this
    decision. Freshness is enforced by PcepPathComputer before calling here.
    """
    return {name: node.loopback for name, node in sorted(topology.nodes.items())
            if policy.source(node.loopback, topology) == node.loopback}


def domain_destinations(topology: Topology, policy: DomainDelegationPolicy) -> tuple[str, ...]:
    """Return current unique domain destinations, including transit routers."""
    return tuple(name for name, node in sorted(topology.nodes.items())
                 if policy.destination(node.loopback, topology))


def _key_fingerprint(algorithm: str, encoded: str) -> str:
    """Validate OpenSSH public-key framing and return its standard fingerprint."""
    try:
        raw = base64.b64decode(encoded, validate=True)
        offset = 0

        def field() -> bytes:
            nonlocal offset
            if offset + 4 > len(raw):
                raise ValueError("Invalid pinned SSH public-key framing")
            size = struct.unpack_from("!I", raw, offset)[0]
            offset += 4
            if not size or size > 16384 or offset + size > len(raw):
                raise ValueError("Invalid pinned SSH public-key framing")
            value = raw[offset:offset + size]
            offset += size
            return value

        if field().decode("ascii") != algorithm:
            raise ValueError("Pinned SSH key algorithm differs from its public key")
        if algorithm == "ssh-ed25519":
            if len(field()) != 32:
                raise ValueError("Invalid pinned Ed25519 public key")
        elif algorithm == "ssh-rsa":
            field()  # exponent
            field()  # modulus
        elif algorithm in {"ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
            if field().decode("ascii") != algorithm.removeprefix("ecdsa-sha2-"):
                raise ValueError("Pinned SSH elliptic curve differs from its algorithm")
            field()
        else:
            raise ValueError("Unsupported pinned SSH public-key algorithm")
        if offset != len(raw):
            raise ValueError("Pinned SSH public key has trailing data")
    except (UnicodeError, struct.error) as exc:
        raise ValueError("Invalid pinned SSH public key") from exc
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def _host_matches(pattern: str, endpoint: str) -> bool:
    if pattern == endpoint:
        return True
    if not pattern.startswith("|1|"):
        return False
    try:
        _, version, salt, expected = pattern.split("|")
        if version != "1":
            return False
        salt_bytes = base64.b64decode(salt, validate=True)
        expected_bytes = base64.b64decode(expected, validate=True)
        if len(salt_bytes) != 20 or len(expected_bytes) != 20:
            return False
        return hmac.compare_digest(hmac.digest(salt_bytes, endpoint.encode(), "sha1"), expected_bytes)
    except (ValueError, UnicodeError):
        return False


def _pinned_keys(path: Path, endpoint: str) -> tuple[str, ...]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PIN_FILE_BYTES:
            raise ValueError("SSH host-key pins must be a bounded regular file")
        raw = handle.read(_MAX_PIN_FILE_BYTES + 1)
    if len(raw) > _MAX_PIN_FILE_BYTES:
        raise ValueError("SSH host-key pins exceed 1 MiB")
    fingerprints = set()
    for line in raw.decode("utf-8").splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        marker = parts.pop(0) if parts[0].startswith("@") else None
        if len(parts) < 3:
            raise ValueError("Malformed SSH host-key pin entry")
        if not any(_host_matches(pattern, endpoint) for pattern in parts[0].split(",")):
            continue
        if marker is not None:
            raise ValueError("SSH endpoint requires direct, unrevoked public-key pins")
        fingerprints.add(_key_fingerprint(parts[1], parts[2]))
    if not fingerprints:
        raise ValueError("The management endpoint has no independently pinned SSH host key")
    return tuple(sorted(fingerprints))


class TransportEnrollmentRegistry:
    """Immutable management profiles with independently rechecked key identity.

    Password values are never read into policy documents. Their environment
    references are policy, and their availability is a readiness condition.
    A pin change alters the revision and requires a newly reviewed plan.
    """

    def __init__(self, devices: Mapping[str, ManagedDevice], known_hosts: Path | None):
        if (len(devices) > 256 or any(not isinstance(device, ManagedDevice) or
                                    device.topology_node_id != node for node, device in devices.items())):
            raise ValueError("Transport enrollments must map distinct topology nodes to management profiles")
        endpoints: set[tuple[str, int]] = set()
        for device in devices.values():
            try:
                host = str(ip_address(device.host))
            except ValueError:
                host = device.host.lower().rstrip(".")
            endpoint = (host, device.port)
            if endpoint in endpoints:
                raise ValueError("Distinct topology nodes cannot share an enrolled management endpoint")
            endpoints.add(endpoint)
        self._devices = MappingProxyType(dict(devices))
        self.known_hosts = Path(known_hosts) if known_hosts is not None else None

    @property
    def devices(self) -> dict[str, ManagedDevice]:
        return dict(self._devices)

    def _snapshot(self, node: str) -> dict[str, Any]:
        device = self._devices.get(node)
        if device is None:
            raise ValueError("The domain headend has no enrolled configuration transport")
        if normalize_device_type(device.device_type) != "cisco_ios":
            raise ValueError("Configuration provisioning is qualified only for the Cisco IOS XE SSH profile")
        if not device.password_env:
            raise ValueError("The enrolled transport has no credential reference")
        if self.known_hosts is None:
            raise ValueError("An independently pinned SSH host-key file is required")
        endpoint = device.host if device.port == 22 else f"[{device.host}]:{device.port}"
        return {"backend": "iosxe-ssh", "device": asdict(device),
                "credential_reference": device.password_env,
                "host_key_fingerprints": list(_pinned_keys(self.known_hosts, endpoint))}

    def snapshot(self, node: str | None = None) -> dict[str, Any]:
        if node is not None:
            return self._snapshot(node)
        result = {}
        for name, device in sorted(self._devices.items()):
            try:
                result[name] = self._snapshot(name)
            except (ValueError, OSError, UnicodeError):
                result[name] = {"backend": "iosxe-ssh", "device": asdict(device),
                                "credential_reference": device.password_env, "host_key_unavailable": True}
        return result

    def revision(self, node: str) -> str:
        return digest(self._snapshot(node))

    def readiness(self, node: str) -> tuple[bool, str]:
        try:
            snapshot = self._snapshot(node)
            if not os.environ.get(snapshot["credential_reference"]):
                raise ValueError("The enrolled transport credential is unavailable")
            return True, ""
        except (ValueError, OSError, UnicodeError) as exc:
            return False, str(exc)[:256]
