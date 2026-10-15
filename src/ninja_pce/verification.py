"""Conservative, version-scoped operational evidence adapters.

Parsers intentionally support a small documented CLI dialect. Unrecognized output
is a verification failure, never permission to infer health from configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Protocol


@dataclass(frozen=True)
class VerificationExpectation:
    identity: str
    destination: str
    bandwidth_bps: int
    hops: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    errors: tuple[str, ...]
    version: str | None = None
    identity: str | None = None
    destination: str | None = None
    bandwidth_bps: int | None = None
    hops: tuple[tuple[str, str], ...] = ()


class OperationalVerifier(Protocol):
    def verify(
        self,
        output_format: str,
        version_output: str,
        outputs: Mapping[str, str],
        expected: VerificationExpectation,
    ) -> VerificationResult: ...


def supported_version(output_format: str, version_output: str) -> str | None:
    patterns = {
        "iosxr": r"(?:IOS XR[^\n]*Version\s*|^\s*Version\s*:\s*)([0-9][A-Za-z0-9.-]*)",
        "iosxe": r"IOS XE[^\n]*Version\s*([0-9][A-Za-z0-9.-]*)",
        "junos": r"^\s*Junos:\s*([0-9][A-Za-z0-9.-]*)",
    }
    releases = {
        "iosxr": r"7\.9\.\d+[A-Za-z0-9.-]*",
        "iosxe": r"(?:17\.12\.\d+[A-Za-z0-9.-]*|16\.09\.07|16\.9\.7)",
        "junos": r"23\.4R\d+[A-Za-z0-9.-]*",
    }
    pattern = patterns.get(output_format)
    values = re.findall(pattern, version_output, re.I | re.M) if pattern else []
    if len(values) != 1 or re.fullmatch(releases[output_format], values[0], re.I) is None:
        return None
    return values[0]


def _single(pattern: str, text: str, flags: int = 0) -> str | None:
    values = re.findall(pattern, text, flags)
    return values[0] if len(values) == 1 else None


def _bandwidth(value: str | None, unit: str = "bps") -> int | None:
    if value is None:
        return None
    scale = {
        "bps": 1,
        "kbps": 1000,
        "kbits": 1000,
        "mbps": 1_000_000,
        "m": 1_000_000,
        "k": 1000,
        "g": 1_000_000_000,
        "": 1,
    }
    try:
        result = Decimal(value) * scale[unit.lower()]
        return int(result) if result.is_finite() and result >= 0 and result == int(result) else None
    except (ValueError, KeyError):
        return None


class CliOperationalVerifier:
    """IOS XR 7.9.x, IOS XE 17.12.x/16.9.7, Junos 23.4R CLI dialects."""

    def verify(
        self,
        output_format: str,
        version_output: str,
        outputs: Mapping[str, str],
        expected: VerificationExpectation,
    ) -> VerificationResult:
        version = supported_version(output_format, version_output)
        if version is None:
            return VerificationResult(False, ("Unsupported or missing platform/software version",))
        # A single detailed LSP response binds all fields to one operational object.
        details = [
            value
            for command, value in outputs.items()
            if "traffic-eng tunnels" in command or "show mpls lsp name" in command
        ]
        if len(details) != 1:
            return VerificationResult(False, ("Missing or ambiguous detailed LSP evidence",), version)
        text = details[0]
        if re.search(
            r"(?:%\s*(?:invalid|error)|syntax error|error:|admission.*fail|insufficient bandwidth)",
            text,
            re.I,
        ):
            return VerificationResult(False, ("Router reports an operational/signaling error",), version)
        errors: list[str] = []
        identity: str | None = None
        destination: str | None = None
        bandwidth: int | None = None
        hops: tuple[tuple[str, str], ...] = ()
        if output_format in {"iosxr", "iosxe"}:
            iosxe_16_9 = output_format == "iosxe" and version in {"16.09.07", "16.9.7"}
            header_pattern = (
                r"^\s*Name:\s*[^\r\n]+?\s+\((Tunnel\d+)\)\s+Destination:\s*"
                r"(\d+\.\d+\.\d+\.\d+)"
                r"(?:\s+Ifhandle:\s*0x[0-9a-fA-F]+(?:\s+\(auto-tunnel for pce client\))?)?\s*$"
                if iosxe_16_9
                else r"^\s*Name:\s*(\S+)(?:\s+\((Tunnel\d+)\))?\s+Destination:\s*"
                r"(\d+\.\d+\.\d+\.\d+)\s*$"
            )
            headers = list(
                re.finditer(header_pattern, text, re.M | re.I)
            )
            header = (
                headers[0]
                if len(headers) == 1 and len(re.findall(r"^\s*Name:", text, re.M | re.I)) == 1
                else None
            )
            if header:
                identity = header.group(1) if iosxe_16_9 else header.group(2) or header.group(1)
                destination = header.group(2) if iosxe_16_9 else header.group(3)
            state_labels = (r"\bAdmin:", r"\bOper:", r"\bPath:", r"\bSignall?ing:")
            if any(len(re.findall(label, text, re.I)) != 1 for label in state_labels) or not re.search(
                r"Admin:\s*up\s+Oper:\s*up\s+Path:\s*valid\s+Signall?ing:\s*connected\b", text, re.I
            ):
                errors.append("Missing positive admin/oper/path/RSVP signaling state")
            bw = _single(r"^\s*Bandwidth:\s*(\d+(?:\.\d+)?)\s*kbps\b", text, re.M | re.I)
            if bw:
                bandwidth = _bandwidth(bw, "kbps")
            if output_format == "iosxr":
                current = re.search(
                    r"Current LSP Info:(.*?)(?:\n\s*(?:History|Last Error|Previous LSP Info):|\Z)",
                    text,
                    re.S | re.I,
                )
                route = (
                    re.search(
                        r"Explicit Route:\s*\n((?:\s*(?:Strict|Loose),\s*\d+\.\d+\.\d+\.\d+\s*\n?)+)",
                        current.group(1),
                        re.I,
                    )
                    if current
                    else None
                )
                if route:
                    hops = tuple(
                        (kind.lower(), ip)
                        for kind, ip in re.findall(
                            r"(Strict|Loose),\s*(\d+\.\d+\.\d+\.\d+)", route.group(1), re.I
                        )
                    )
                if len(re.findall(r"Current LSP Info:", text, re.I)) != 1 or (
                    current and len(re.findall(r"Explicit Route:", current.group(1), re.I)) != 1
                ):
                    errors.append("Ambiguous current LSP explicit route")
                current_text = current.group(1) if current else ""
                tspec = _single(r"Tspec:\s*avg rate\s*=\s*(\d+(?:\.\d+)?)\s*kbits\b", current_text, re.I)
                fspec_rate = _single(
                    r"Resv Info:.*?Fspec:\s*avg rate\s*=\s*(\d+(?:\.\d+)?)\s*kbits\b",
                    current_text,
                    re.S | re.I,
                )
                if (
                    _bandwidth(tspec, "kbits") != expected.bandwidth_bps
                    or _bandwidth(fspec_rate, "kbits") != expected.bandwidth_bps
                ):
                    errors.append("Missing or mismatched current RSVP Tspec/Resv Fspec")
                if (
                    len(re.findall(r"\bTspec:", current_text, re.I)) != 1
                    or len(re.findall(r"\bFspec:", current_text, re.I)) != 1
                ):
                    errors.append("Ambiguous current RSVP bandwidth evidence")
            else:
                path_sections = re.findall(
                    r"^\s*RSVP Path Info:\s*\n(.*?)(?=^\s*RSVP Resv Info:|\Z)",
                    text,
                    re.S | re.M | re.I,
                )
                resv_sections = re.findall(
                    r"^\s*RSVP Resv Info:\s*\n(.*?)(?=^\s*(?:Shortest Unconstrained Path Info|History):|\Z)",
                    text,
                    re.S | re.M | re.I,
                )
                path_text = path_sections[0] if len(path_sections) == 1 else ""
                resv_text = resv_sections[0] if len(resv_sections) == 1 else ""
                ipv4 = r"\d+\.\d+\.\d+\.\d+"
                route = re.search(
                    rf"^[ \t]*Explicit Route:[ \t]*({ipv4}(?:[ \t]+{ipv4})*"
                    rf"(?:[ \t]*\n[ \t]+{ipv4}(?:[ \t]+{ipv4})*)*)[ \t]*$",
                    path_text,
                    re.M | re.I,
                )
                if route:
                    hops = tuple(
                        ("strict", ip) for ip in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", route.group(1))
                    )
                    # This image appends the destination loopback to its RSVP
                    # display, although the computed strict ERO ends at the
                    # tail-end's incoming interface. Remove only that exact
                    # terminal endpoint, never a mismatched or internal hop.
                    if iosxe_16_9 and hops == (*expected.hops, ("strict", expected.destination)):
                        hops = hops[:-1]
                if (
                    len(path_sections) != 1
                    or len(resv_sections) != 1
                    or len(re.findall(r"RSVP Path Info:", text, re.I)) != 1
                    or len(re.findall(r"RSVP Resv Info:", text, re.I)) != 1
                    or len(re.findall(r"Explicit Route:", path_text, re.I)) != 1
                ):
                    errors.append("Ambiguous RSVP path explicit route")
                # Configured bandwidth alone is insufficient: require matching
                # current Path Tspec and Resv Fspec, bound to their sections.
                tspec = _single(
                    r"Tspec:\s*ave rate\s*=\s*(\d+(?:\.\d+)?)\s*kbits\b", path_text, re.I
                )
                fspec = re.search(
                    r"Fspec:\s*ave rate\s*=\s*(\d+(?:\.\d+)?)\s*kbits\b", resv_text, re.I
                )
                if (
                    not fspec
                    or len(re.findall(r"\bFspec:", resv_text, re.I)) != 1
                    or _bandwidth(fspec.group(1), "kbits") != expected.bandwidth_bps
                ):
                    errors.append("Missing or mismatched RSVP reservation Fspec")
                if _bandwidth(tspec, "kbits") != expected.bandwidth_bps:
                    errors.append("Missing or mismatched RSVP Path Tspec")
        elif output_format == "junos":
            identity = _single(r"^\s*LSPname:\s*(\S+)\s*$", text, re.M)
            destination = _single(r"^\s*To:\s*(\d+\.\d+\.\d+\.\d+),\s*From:", text, re.M)
            if _single(r"^\s*LSPstate:\s*(\w+)\b", text, re.M) != "Up" or not re.search(
                r"^\s*ActivePath:\s*\S+\s*\(primary\)", text, re.M
            ):
                errors.append("Missing positive LSP/active primary state")
            if len(re.findall(r"^\s*ActivePath:", text, re.M)) != 1:
                errors.append("Ambiguous active primary path")
            if _single(r"^\s*Primary\s+\S+\s+State:\s*(\w+)\b", text, re.M) != "Up":
                errors.append("Missing positive RSVP primary signaling state")
            bw_match = re.search(r"^\s*Bandwidth:\s*(\d+(?:\.\d+)?)([kmg]?)bps\b", text, re.M | re.I)
            if bw_match:
                bandwidth = _bandwidth(bw_match.group(1), (bw_match.group(2) + "bps").lower())
            if len(re.findall(r"^\s*Bandwidth:", text, re.M | re.I)) != 1:
                errors.append("Ambiguous signaled bandwidth")
            route = re.search(r"Computed ERO[^\n]*\n\s*((?:(?:\d+\.\d+\.\d+\.\d+)[SL]\s*)+)", text)
            if route:
                hops = tuple(
                    ("strict" if kind == "S" else "loose", ip)
                    for ip, kind in re.findall(r"(\d+\.\d+\.\d+\.\d+)([SL])", route.group(1))
                )
            if len(re.findall(r"Computed ERO", text)) != 1:
                errors.append("Ambiguous computed ERO")
            rro = _single(r"^\s*Received RRO[^\n]*\n[ \t]*((?:\d+\.\d+\.\d+\.\d+[ \t]*)+)", text, re.M)
            actual_recorded = tuple(re.findall(r"\d+\.\d+\.\d+\.\d+", rro or ""))
            if actual_recorded != tuple(ip for _, ip in expected.hops):
                errors.append("Missing or mismatched received RSVP record route")
        if identity != expected.identity:
            errors.append("Missing, ambiguous or mismatched LSP identity")
        if destination != expected.destination:
            errors.append("Missing or mismatched LSP destination")
        if bandwidth != expected.bandwidth_bps:
            errors.append("Missing or mismatched signaled bandwidth")
        if hops != expected.hops:
            errors.append("Missing or mismatched current explicit route")
        if any(kind == "loose" for kind, _ in expected.hops):
            errors.append("Loose ERO does not prove the selected directed link path")
        return VerificationResult(not errors, tuple(errors), version, identity, destination, bandwidth, hops)
