from __future__ import annotations

import json
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

    return {
        "topology": topology,
        "demand": demand,
        "output": demand.output,
        "source_node": source_node,
        "dest_node": dest_node,
        "result": result,
        "bandwidth_kbps": int(demand.bandwidth_mbps * 1000),
        "bandwidth_bps": int(demand.bandwidth_mbps * 1_000_000),
        "bandwidth_junos": f"{demand.bandwidth_mbps:g}m",
        "path_string": " -> ".join(result.path_nodes),
    }


def render_config(result: CspfResult, topology: Topology, output_format: str) -> str:
    fmt = output_format.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported output format: {output_format}. Supported: {sorted(SUPPORTED_FORMATS)}")

    template_name = {
        "generic": "generic_explicit_path.j2",
        "iosxr": "iosxr_rsvp_te.j2",
        "iosxe": "iosxe_rsvp_te.j2",
        "junos": "junos_rsvp_te.j2",
    }[fmt]
    template = _env().get_template(template_name)
    return template.render(**build_render_context(result, topology))


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
        verify_path.write_text(render_template(result, topology, "verification_commands.txt.j2"), encoding="utf-8")
        written.append(verify_path)

    summary_path = out / f"{base}_summary.json"
    summary_path.write_text(json.dumps(result.to_summary_dict(), indent=2), encoding="utf-8")
    written.append(summary_path)
    return written
