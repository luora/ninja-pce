from __future__ import annotations

import json
import re
from decimal import Decimal
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import CspfResult, Topology

SUPPORTED_FORMATS = {"generic", "iosxr", "iosxe", "junos"}


def _env() -> Environment:
    return Environment(
        loader=PackageLoader("ninja_pce", "templates"),
        autoescape=select_autoescape(enabled_extensions=()),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_render_context(result: CspfResult, topology: Topology) -> dict[str, Any]:
    source_node = topology.nodes[result.demand.source]
    dest_node = topology.nodes[result.demand.destination]
    demand = result.demand
    # Validate here as well as in YAML loaders: public callers can construct models.
    for value in (
        demand.name,
        demand.source,
        demand.destination,
        demand.output.tunnel_name,
        demand.output.explicit_path_name,
        demand.output.junos_lsp_name,
        demand.output.junos_primary_path_name,
    ):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError(f"Unsafe command identifier: {value!r}")
    if not re.fullmatch(r"tunnel-te[0-9]{1,5}", demand.output.iosxr_interface):
        raise ValueError("IOS XR interface must be tunnel-te followed by a tunnel ID")
    if type(demand.output.tunnel_id) is not int or not 0 <= demand.output.tunnel_id <= 65535:
        raise ValueError("Tunnel ID must be an integer in 0..65535")
    for priority in (demand.setup_priority, demand.hold_priority):
        if type(priority) is not int or not 0 <= priority <= 7:
            raise ValueError("RSVP priorities must be integers in 0..7")
    for node in (source_node, dest_node):
        IPv4Address(node.loopback)
    if len(result.explicit_hops) != len(result.path_links):
        raise ValueError("ERO must identify every selected directed link")
    for hop, link in zip(result.explicit_hops, result.path_links):
        if hop.link_id != link.id or hop.node_id != link.target or hop.local_address != link.from_ip:
            raise ValueError("ERO hop does not match the selected directed link identity")
        address = getattr(hop, "address", None)
        if hop.hop_type not in {"strict", "loose"}:
            raise ValueError("Unsupported ERO hop type")
        expected = link.to_ip if hop.hop_type == "strict" else hop.loopback
        if address is None or address != expected:
            raise ValueError(
                f"Unsupported strict/unnumbered ERO for link {link.id}: missing or incorrect address"
            )
        IPv4Address(address)
        if hop.hop_type == "strict":
            directional = getattr(topology, "directional_links", topology.links)
            same_address = [
                edge for edge in directional if edge.source == link.source and edge.to_ip == address
            ]
            if len(same_address) > 1:
                raise ValueError(f"Strict ERO address {address} does not uniquely identify link {link.id}")
    bandwidth = Decimal(str(demand.bandwidth_mbps))
    if not bandwidth.is_finite() or bandwidth < 0:
        raise ValueError("Bandwidth must be finite and nonnegative")
    bandwidth_bps = demand.bandwidth_bps
    bandwidth_kbps = (bandwidth_bps + 999) // 1000

    return {
        "topology": topology,
        "demand": demand,
        "output": demand.output,
        "source_node": source_node,
        "dest_node": dest_node,
        "result": result,
        "bandwidth_kbps": bandwidth_kbps,
        "bandwidth_bps": bandwidth_bps,
        "bandwidth_junos": str(bandwidth_bps),
        "path_string": " -> ".join(result.path_nodes),
    }


def render_config(result: CspfResult, topology: Topology, output_format: str) -> str:
    fmt = output_format.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported output format: {output_format}. Supported: {sorted(SUPPORTED_FORMATS)}"
        )

    template_name = {
        "generic": "generic_explicit_path.j2",
        "iosxr": "iosxr_rsvp_te.j2",
        "iosxe": "iosxe_rsvp_te.j2",
        "junos": "junos_rsvp_te.j2",
    }[fmt]
    context = build_render_context(result, topology)
    if fmt in {"iosxr", "iosxe"}:
        signaled = context["bandwidth_kbps"] * 1000
        if any(signaled > link.available_bw_bps for link in result.path_links):
            raise ValueError("Rounded Cisco kbps reservation exceeds selected link bandwidth")
    template = _env().get_template(template_name)
    return template.render(**context)


def render_template(result: CspfResult, topology: Topology, template_name: str) -> str:
    template = _env().get_template(template_name)
    return template.render(**build_render_context(result, topology))


def write_outputs(result: CspfResult, topology: Topology, output_dir: str | Path) -> list[Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    base = result.demand.output.tunnel_name

    for fmt in result.demand.output.formats:
        text = render_config(result, topology, fmt)
        path = out / f"{base}_{fmt}.txt"
        path.write_text(text, encoding="utf-8")
        written.append(path)

    if result.demand.output.write_plan:
        plan_path = out / f"{base}_change_plan.md"
        plan_path.write_text(render_template(result, topology, "change_plan.md.j2"), encoding="utf-8")
        written.append(plan_path)

    if result.demand.output.write_verification:
        verify_path = out / f"{base}_verification_commands.txt"
        verify_path.write_text(
            render_template(result, topology, "verification_commands.txt.j2"), encoding="utf-8"
        )
        written.append(verify_path)

    summary_path = out / f"{base}_summary.json"
    summary_path.write_text(json.dumps(result.to_summary_dict(), indent=2), encoding="utf-8")
    written.append(summary_path)
    return written
