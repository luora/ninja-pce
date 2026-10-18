from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import __version__
from .application import add_application_commands, application_command
from .controller import add_controller_commands, controller_command
from .cspf import CspfNoPath, CspfSearchLimit, compute_cspf
from .daemon import HealthState, load_daemon_config, run_daemon_cycle, start_health_server
from .inventory import device_for_node, validate_inventory
from .live import add_live_commands, collect_bgpls, listen_pcep
from .loaders import InputError, load_demand, load_inventory, load_topology
from .reconcile import file_fingerprint, reconcile_once
from .render import write_outputs
from .runtime import add_runtime_commands, runtime_command
from .southbound import (
    SouthboundError,
    build_push_plan,
    require_approved_execution,
)
from .startup import (
    PERMANENT_EXIT,
    PermanentStartupError,
    listener_startup_error,
    permanent_os_error,
    startup_finding,
)


def build_parser() -> argparse.ArgumentParser:
    from .admin import add_admin_commands
    parser = argparse.ArgumentParser(
        prog="ninja-pce",
        description="CSPF, topology reconciliation, and approved RSVP-TE config delivery.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    compute = sub.add_parser("compute", help="Compute CSPF path and generate dry-run configs")
    compute.add_argument("--topology", required=True, help="Topology YAML/JSON file")
    compute.add_argument("--demand", required=True, help="Service demand YAML/JSON file")
    compute.add_argument("--output-dir", default="build/outputs", help="Directory for generated outputs")
    compute.add_argument("--show-pruned", action="store_true", help="Print pruned links and reasons")
    compute.add_argument("--show-candidates", action="store_true", help="Print all candidate paths")

    validate = sub.add_parser("validate", help="Validate input files without writing outputs")
    validate.add_argument("--topology", required=True, help="Topology YAML/JSON file")
    validate.add_argument("--demand", required=True, help="Service demand YAML/JSON file")
    validate.add_argument("--inventory", help="Optional host/OS inventory YAML/JSON file")

    inventory = sub.add_parser(
        "inventory-validate",
        help="Validate that every topology node has one supported inventory entry",
    )
    inventory.add_argument("--topology", required=True, help="Topology YAML/JSON file")
    inventory.add_argument("--inventory", required=True, help="Host/OS inventory YAML/JSON file")

    push = sub.add_parser(
        "push",
        help="Preview or explicitly approve a Netmiko configuration push",
    )
    _add_delivery_arguments(push)

    reconcile = sub.add_parser(
        "reconcile",
        help="Recompute and deliver config only when topology-driven desired state changes",
    )
    _add_delivery_arguments(reconcile)
    reconcile.add_argument(
        "--state",
        help="Reconciliation state JSON. Defaults under --output-dir",
    )
    reconcile.add_argument("--force", action="store_true", help="Reconcile even if inputs are unchanged")
    reconcile.add_argument(
        "--watch",
        action="store_true",
        help="Keep watching the topology file and reconcile after changes",
    )
    reconcile.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Watch polling interval in seconds. Default: 1",
    )
    reconcile.add_argument(
        "--debounce",
        type=float,
        default=2.0,
        help="Seconds a changed topology file must remain stable. Default: 2",
    )

    topo = sub.add_parser("topology-summary", help="Print a human-readable TED summary")
    topo.add_argument("--topology", required=True, help="Topology YAML/JSON file")

    lab = sub.add_parser("gns3-demo", help="Run the bundled GNS3 demo scenarios")
    lab.add_argument("--lab-root", default="/lab", help="Lab data root. Default: /lab")
    lab.add_argument("--output-dir", default="/outputs", help="Output root. Default: /outputs")
    lab.add_argument("--show-pruned", action="store_true", help="Print pruned links and reasons")
    lab.add_argument("--show-candidates", action="store_true", help="Print candidate paths")

    lab_render = sub.add_parser("lab-render", help="Validate and render a data-driven lab extension without device access")
    lab_render.add_argument("--profile", required=True, help="Explicit platform and topology intent YAML/JSON")
    lab_render.add_argument("--output-dir", default="build/multi-area", help="Directory for reviewed configuration artifacts")
    lab_render.add_argument("--bgpls-asn", type=int, help="Explicit observed AS for optional ABR identity policy")
    lab_render.add_argument("--bgpls-instance-id", type=int, help="Explicit observed BGP-LS instance ID")
    lab_render.add_argument("--bgpls-identifier", type=int, help="Explicit observed BGP-LS identifier")

    daemon = sub.add_parser(
        "daemon",
        help="Run multi-demand topology reconciliation from a daemon config",
    )
    daemon.add_argument("--config", required=True, help="Daemon YAML/JSON config file")
    daemon.add_argument("--once", action="store_true", help="Run one reconciliation cycle and exit")
    daemon.add_argument(
        "--poll-interval",
        type=float,
        help="Override config poll_interval in seconds",
    )
    daemon.add_argument("--health-host", help="Override health endpoint host")
    daemon.add_argument("--health-port", type=int, help="Override health endpoint port")
    daemon.add_argument("--no-health", action="store_true", help="Disable the health/metrics HTTP server")
    dashboard = sub.add_parser("dashboard", help="Serve a read-only controller web dashboard")
    dashboard.add_argument("--host", default="127.0.0.1", help="Bind address; default is loopback only")
    dashboard.add_argument("--port", type=int, default=8088, help="Dashboard HTTP port (default: 8088)")
    dashboard.add_argument("--topology", default="build/live/topology.json", help="Normalized live topology JSON")
    dashboard.add_argument("--bgpls-status", default="build/live/bgpls-status.json", help="Collector health JSON")
    dashboard.add_argument("--bgpls-raw", default="build/live/bgpls-raw.json", help="Raw collector JSON")
    dashboard.add_argument("--ospf-multi-area-policy", help="Explicit JSON policy matching the BGP-LS collector")
    dashboard.add_argument("--pcep", default="build/live/pcep.json", help="Checksummed PCEP observation JSON")
    dashboard.add_argument("--state-dir", default="build/state", help="Existing shared reservation directory")
    dashboard.add_argument("--max-age", type=float, default=30.0, help="Observation freshness limit in seconds")
    dashboard.add_argument("--refresh", type=float, default=5.0, help="Browser polling interval in seconds")
    dashboard.add_argument("--demo", action="store_true", help="Use labelled synthetic examples, without live files")
    dashboard.add_argument("--operating-mode", choices=("observe", "pcep-control"), default="pcep-control",
                           help="Readiness mode; observe intentionally disables PCEP requirements")
    dashboard.add_argument("--bgpls-peer", action="append", default=[],
                           help="Required trusted BGP-LS peer; repeat for each peer")
    dashboard.add_argument("--controller-config", help="Managed controller configuration for process readiness")
    dashboard.add_argument("--controller-digest", help="Expected digest of the managed controller configuration")
    dashboard.add_argument("--managed-startup", action="store_true",
                           help="Return exit status 78 for permanent managed-service startup errors")
    add_live_commands(sub)
    add_controller_commands(sub)
    add_application_commands(sub)
    add_runtime_commands(sub)
    add_admin_commands(sub)
    return parser


def _add_delivery_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--topology", required=True, help="Topology YAML/JSON file")
    parser.add_argument("--demand", required=True, help="Service demand YAML/JSON file")
    parser.add_argument("--inventory", required=True, help="Host/OS inventory YAML/JSON file")
    parser.add_argument("--output-dir", default="build/outputs", help="Generated output directory")
    parser.add_argument(
        "--state-dir", default=os.environ.get("NINJA_PCE_STATE_DIR", "build/state"),
        help="Shared local reservation/state directory for every controlled demand (default: build/state)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Connect with Netmiko and apply the config; otherwise preview only",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve execution; valid only with --execute",
    )
    parser.add_argument(
        "--approved-plan-id",
        help="Exact plan ID shown by a reviewed preview; rejects stale approval",
    )
    parser.add_argument(
        "--approval-expires-at",
        help="Optional timezone-aware ISO timestamp bounding this exact approval",
    )


def cmd_compute(args: argparse.Namespace) -> int:
    topology = load_topology(args.topology)
    demand = load_demand(args.demand)
    result = compute_cspf(topology, demand)
    written = write_outputs(result, topology, args.output_dir)

    print("ninja-pce CSPF result")
    print("=====================")
    print(f"Topology:      {topology.name}")
    print(f"Service:       {demand.name}")
    print(f"Bandwidth:     {demand.bandwidth_mbps:g} Mbps")
    print(f"Path:          {' -> '.join(result.path_nodes)}")
    print(f"Links:         {' -> '.join(link.id for link in result.path_links)}")
    print(f"Metric:        {result.total_metric}")
    print(f"Bottleneck BW: {result.bottleneck_available_bw_mbps:g} Mbps")
    print(f"Residual BW:   {result.residual_after_reservation_mbps:g} Mbps")
    print(f"Candidates:    {len(result.candidate_paths)}")
    print(f"Dry-run only:  {demand.output.dry_run}")
    print()

    if args.show_candidates:
        print("Candidate paths:")
        for candidate in result.candidate_paths:
            print(
                f"- #{candidate.rank}: {' -> '.join(candidate.path_nodes)} "
                f"metric={candidate.total_metric} hops={candidate.hop_count} "
                f"bottleneck={candidate.bottleneck_available_bw_mbps:g}Mbps "
                f"residual={candidate.residual_after_reservation_mbps:g}Mbps"
            )
        print()

    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")
        print()

    print("Generated files:")
    for path in written:
        print(f"- {Path(path)}")

    if args.show_pruned and result.pruned_links:
        print()
        print("Pruned links:")
        for item in result.pruned_links:
            print(f"- {item['id']} {item['from']}->{item['to']}: {item['reason']}")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    topology = load_topology(args.topology)
    demand = load_demand(args.demand)
    if args.inventory:
        inventory = load_inventory(args.inventory)
        validate_inventory(inventory, topology, require_all_nodes=True)
    result = compute_cspf(topology, demand)
    print("Input validation passed")
    print(f"Topology: {topology.name}")
    print(f"Nodes:    {len(topology.nodes)}")
    print(f"Links:    {len(topology.links)}")
    print(f"Demand:   {demand.name}")
    print(f"Path:     {' -> '.join(result.path_nodes)}")
    print(f"Candidates available: {len(result.candidate_paths)}")
    if args.inventory:
        print(f"Inventory devices: {len(inventory.devices)}")
    return 0


def cmd_inventory_validate(args: argparse.Namespace) -> int:
    topology = load_topology(args.topology)
    inventory = load_inventory(args.inventory)
    mapped = validate_inventory(inventory, topology, require_all_nodes=True)
    print("Inventory validation passed")
    print(f"Topology: {topology.name}")
    print(f"Nodes:    {len(topology.nodes)}")
    print(f"Devices:  {len(inventory.devices)}")
    print(f"Mapped:   {', '.join(mapped)}")
    return 0


def cmd_lab_render(args: argparse.Namespace) -> int:
    from .lab_profile import (
        lab_ospf_multi_area_policy,
        lab_profile_summary,
        load_lab_profile,
        render_lab_configs,
    )
    from .live import _write_document
    from .state import digest

    source = Path(args.profile)
    profile = load_lab_profile(source)
    configs = render_lab_configs(profile)
    output = Path(args.output_dir)
    summary = lab_profile_summary(profile)
    summary["profile_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    summary["configuration_sha256"] = {
        node: hashlib.sha256(config.encode()).hexdigest() for node, config in sorted(configs.items())
    }
    domain = (args.bgpls_asn, args.bgpls_instance_id, args.bgpls_identifier)
    policy = None
    if any(value is not None for value in domain):
        if any(value is None for value in domain):
            raise InputError("BGP-LS policy generation requires AS, instance ID and identifier together")
        policy = lab_ospf_multi_area_policy(
            profile, autonomous_system=args.bgpls_asn, instance_id=args.bgpls_instance_id,
            bgpls_identifier=args.bgpls_identifier,
        )
        summary["ospf_multi_area_policy_digest"] = digest(policy)
    node_map = {str(node.loopback): node.id for node in profile.nodes}
    summary["node_map_digest"] = digest(node_map)
    summary["plan_id"] = digest(summary)
    targets = [output / (node + ".cfg") for node in configs] + [
        output / "plan.json", output / "node-map.json", output / "ospf-multi-area-policy.json",
    ]
    if source.resolve() in {target.resolve() for target in targets}:
        raise InputError("Lab profile input and generated outputs must be distinct")
    output.mkdir(parents=True, exist_ok=True)
    for node, config in configs.items():
        target = output / (node + ".cfg")
        if target.is_symlink():
            raise InputError("Lab configuration output must not be a symlink")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(config)
    _write_document(output / "node-map.json", node_map)
    if policy is not None:
        _write_document(output / "ospf-multi-area-policy.json", policy)
    else:
        (output / "ospf-multi-area-policy.json").unlink(missing_ok=True)
    _write_document(output / "plan.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


def _load_delivery(args: argparse.Namespace):
    require_approved_execution(args.execute, args.approve)
    topology = load_topology(args.topology)
    demand = load_demand(args.demand)
    inventory = load_inventory(args.inventory)
    validate_inventory(inventory, topology, require_all_nodes=True)
    result = compute_cspf(topology, demand)
    device = device_for_node(inventory, demand.source)
    plan = build_push_plan(result, topology, device)
    return topology, demand, inventory, result, plan


def cmd_push(args: argparse.Namespace) -> int:
    require_approved_execution(args.execute, args.approve)
    outcome = reconcile_once(
        topology_path=args.topology,
        demand_path=args.demand,
        inventory_path=args.inventory,
        output_dir=args.output_dir,
        state_path=_state_path(args),
        execute=args.execute,
        approved=args.approve,
        approved_plan_id=args.approved_plan_id,
        approval_expires_at=args.approval_expires_at,
    )
    result, plan = outcome.result, outcome.plan
    demand = result.demand
    written = outcome.written_outputs

    print("ninja-pce Netmiko delivery plan")
    print("===============================")
    print(f"Service:   {demand.name}")
    print(f"Path:      {' -> '.join(result.path_nodes)}")
    print(f"Device:    {plan.device.name} ({plan.device.host}:{plan.device.port})")
    print(f"Platform:  {plan.device.device_type}")
    print(f"Renderer:  {plan.output_format}")
    print(f"Plan ID:   {plan.plan_id}")
    print(f"State:     {outcome.action}")
    print(f"Mode:      {'EXECUTE' if args.execute else 'PREVIEW ONLY'}")
    print()
    print("Configuration commands:")
    for command in plan.commands:
        print(f"  {command}")

    if not args.execute:
        print()
        print("No connection made. Use --execute --approve after reviewing the generated files.")
    else:
        pushed = outcome.push_result
        print()
        if pushed:
            print(f"{pushed.status}: {pushed.commands_sent} commands sent to {pushed.device}")
            for error in pushed.verification_errors:
                print(f"Verification: {error}")

    print()
    print("Generated files:")
    for path in written:
        print(f"- {path}")
    return 1 if outcome.push_result and outcome.push_result.status != "VERIFIED" else 0


def _state_path(args: argparse.Namespace) -> Path:
    if getattr(args, "state", None):
        return Path(args.state)
    demand_stem = Path(args.demand).stem
    safe_stem = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in demand_stem
    )
    identity = hashlib.sha256(str(Path(args.demand).resolve()).encode()).hexdigest()[:12]
    return Path(args.state_dir) / f".ninja-pce-{safe_stem}-{identity}-state.json"


def _run_reconcile(args: argparse.Namespace, *, force: bool | None = None) -> int:
    outcome = reconcile_once(
        topology_path=args.topology,
        demand_path=args.demand,
        inventory_path=args.inventory,
        output_dir=args.output_dir,
        state_path=_state_path(args),
        execute=args.execute,
        approved=args.approve,
        approved_plan_id=args.approved_plan_id,
        approval_expires_at=args.approval_expires_at,
        force=args.force if force is None else force,
    )
    print("ninja-pce reconciliation")
    print("========================")
    print(f"Action:            {outcome.action}")
    print(f"Topology changed:  {outcome.topology_changed}")
    print(f"Config changed:    {outcome.desired_config_changed}")
    print(f"Path:              {' -> '.join(outcome.result.path_nodes)}")
    print(f"Device:            {outcome.plan.device.name}")
    print(f"State:             {outcome.state_path}")
    print(f"Plan ID:           {outcome.plan.plan_id}")
    if outcome.diff:
        print()
        print("Config diff:")
        print(outcome.diff, end="" if outcome.diff.endswith("\n") else "\n")
    if outcome.action == "preview":
        print()
        print("Preview only. Use --execute --approve to apply the pending desired config.")
    if outcome.push_result:
        print()
        print(
            f"{outcome.push_result.status}: {outcome.push_result.commands_sent} commands sent to "
            f"{outcome.push_result.device}"
        )
        for command, output in outcome.push_result.postcheck_output:
            print(f"\nPost-check: {command}\n{output}")
        for error in outcome.push_result.verification_errors:
            print(f"\nPost-check warning: {error}")
    return 1 if outcome.push_result and outcome.push_result.status != "VERIFIED" else 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    require_approved_execution(args.execute, args.approve)
    if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
        raise InputError("--poll-interval must be positive")
    if not math.isfinite(args.debounce) or args.debounce < 0:
        raise InputError("--debounce cannot be negative")

    result_code = _run_reconcile(args)
    if not args.watch:
        return result_code

    watched = Path(args.topology)
    last_raw_hash = file_fingerprint(watched)
    pending_hash: str | None = None
    changed_at = 0.0
    print()
    print(
        f"Watching {watched} every {args.poll_interval:g}s "
        f"with {args.debounce:g}s debounce. Press Ctrl-C to stop."
    )
    try:
        while True:
            time.sleep(args.poll_interval)
            current_hash = file_fingerprint(watched)
            if current_hash != last_raw_hash:
                last_raw_hash = current_hash
                pending_hash = current_hash
                changed_at = time.monotonic()
                continue
            if pending_hash and time.monotonic() - changed_at >= args.debounce:
                print()
                print("Topology file stabilized; reconciling.")
                _run_reconcile(args, force=False)
                pending_hash = None
    except KeyboardInterrupt:
        print("\nStopped topology watch.")
    return 0


def cmd_topology_summary(args: argparse.Namespace) -> int:
    topology = load_topology(args.topology)
    print("ninja-pce TED summary")
    print("=====================")
    print(f"Topology: {topology.name}")
    print(f"Nodes:    {len(topology.nodes)}")
    print(f"Links:    {len(topology.links)}")
    print()
    print("Nodes:")
    for node in topology.nodes.values():
        print(f"- {node.id:6} loopback={node.loopback:15} role={node.role:3} area={node.area or '-'} tags={','.join(node.tags) or '-'}")
    print()
    print("Links:")
    for link in topology.links:
        print(
            f"- {link.id:12} {link.source:4}->{link.target:4} "
            f"metric={link.metric:<3} reservable={link.reservable_bw_mbps:g}Mbps "
            f"reserved={link.reserved_bw_mbps:g}Mbps available={link.available_bw_mbps:g}Mbps "
            f"groups={','.join(link.admin_groups) or '-'}"
        )
    return 0


def cmd_gns3_demo(args: argparse.Namespace) -> int:
    lab_root = Path(args.lab_root)
    topology = lab_root / "topologies" / "gns3_multiarea.yml"
    scenarios = [
        ("base", lab_root / "demands" / "customer-a-50m.yml"),
        ("avoid-pe1-p3", lab_root / "demands" / "customer-a-50m-avoid-pe1-p3.yml"),
        ("customer-b-120m", lab_root / "demands" / "customer-b-120m.yml"),
    ]
    rc = 0
    for label, demand in scenarios:
        print()
        print(f"=== GNS3 demo scenario: {label} ===")
        ns = argparse.Namespace(
            topology=str(topology),
            demand=str(demand),
            output_dir=str(Path(args.output_dir) / label),
            show_pruned=args.show_pruned,
            show_candidates=args.show_candidates,
        )
        try:
            cmd_compute(ns)
        except (InputError, CspfNoPath, ValueError) as exc:
            rc = 1
            print(f"Scenario {label} failed: {exc}", file=sys.stderr)
    return rc


def _print_daemon_cycle(cycle) -> None:
    print("ninja-pce daemon cycle")
    print("======================")
    print(f"Status:          {cycle.status}")
    print(f"Topology:        {cycle.topology_path}")
    print(f"Topology fresh:  {cycle.topology_fresh}")
    if cycle.topology_age_seconds is not None:
        print(f"Topology age:    {cycle.topology_age_seconds:.1f}s")
    if cycle.message:
        print(f"Message:         {cycle.message}")
    print(f"Demands:         {len(cycle.demand_results)}")
    for result in cycle.demand_results:
        label = result.demand_name or result.demand_path.name
        path = " -> ".join(result.path_nodes) if result.path_nodes else "-"
        print(
            f"- {label}: action={result.action} auto={result.auto_approved} "
            f"device={result.device or '-'} path={path}"
        )
        if result.policy_reasons:
            print("  policy: " + "; ".join(result.policy_reasons))
        if result.error:
            print("  error: " + result.error)


def cmd_daemon(args: argparse.Namespace) -> int:
    config = load_daemon_config(args.config)
    if args.poll_interval is not None:
        if not math.isfinite(args.poll_interval) or args.poll_interval <= 0:
            raise InputError("--poll-interval must be finite and positive")
        config = replace(config, poll_interval=args.poll_interval)
    if args.health_host or args.health_port is not None or args.no_health:
        if args.health_port is not None and not 1 <= args.health_port <= 65535:
            raise InputError("--health-port must be from 1 through 65535")
        health = replace(
            config.health,
            host=args.health_host or config.health.host,
            port=args.health_port if args.health_port is not None else config.health.port,
            enabled=False if args.no_health else config.health.enabled,
        )
        config = replace(config, health=health)

    health_state = HealthState()
    server = None
    if config.health.enabled and not args.once:
        server = start_health_server(config.health, health_state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(
            f"Health endpoint listening on http://{config.health.host}:{config.health.port}/healthz"
        )

    try:
        while True:
            cycle = run_daemon_cycle(config)
            health_state.record(cycle)
            _print_daemon_cycle(cycle)
            if args.once:
                print()
                print(json.dumps(cycle.to_dict(), indent=2, sort_keys=True))
                return 0 if cycle.status == "ok" else 1
            time.sleep(config.poll_interval)
    except KeyboardInterrupt:
        print("\nStopped daemon.")
        return 0
    finally:
        if server is not None:
            server.shutdown()


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import DashboardConfig, create_dashboard_server
    from .live import _ospf_multi_area_policy

    config = DashboardConfig(
        topology_path=Path(args.topology), bgpls_status_path=Path(args.bgpls_status),
        bgpls_raw_path=Path(args.bgpls_raw), pcep_path=Path(args.pcep), state_dir=Path(args.state_dir),
        host=args.host, port=args.port, max_age_seconds=args.max_age, refresh_seconds=args.refresh, demo=args.demo,
        ospf_multi_area=_ospf_multi_area_policy(args.ospf_multi_area_policy),
        operating_mode=getattr(args, "operating_mode", "pcep-control"),
        required_bgpls_peers=tuple(getattr(args, "bgpls_peer", ())),
        controller_config_path=(Path(args.controller_config)
                                if getattr(args, "controller_config", None) else None),
        controller_config_digest=getattr(args, "controller_digest", None),
    )
    try:
        server = create_dashboard_server(config)
    except OSError as exc:
        if permanent_os_error(exc):
            raise listener_startup_error(exc, config.host, config.port, component="dashboard") from exc
        raise ValueError(f"Cannot bind dashboard listener: {exc}") from exc
    print(f"Ninja-PCE dashboard: http://{config.host}:{server.server_address[1]}/", flush=True)
    print("Synthetic demo data" if config.demo else "Read-only controller observations", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "compute":
            return cmd_compute(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "inventory-validate":
            return cmd_inventory_validate(args)
        if args.command == "push":
            return cmd_push(args)
        if args.command == "reconcile":
            return cmd_reconcile(args)
        if args.command == "topology-summary":
            return cmd_topology_summary(args)
        if args.command == "gns3-demo":
            return cmd_gns3_demo(args)
        if args.command == "lab-render":
            return cmd_lab_render(args)
        if args.command == "daemon":
            return cmd_daemon(args)
        if args.command == "dashboard":
            return cmd_dashboard(args)
        if args.command == "bgpls-collect":
            return collect_bgpls(args)
        if args.command == "pcep-listen":
            return listen_pcep(args)
        if args.command == "controller":
            return controller_command(args)
        if args.command == "app":
            return application_command(args)
        if args.command == "runtime":
            return runtime_command(args)
        if args.command == "admin":
            from .admin import admin_command

            return admin_command(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except CspfSearchLimit as exc:
        print(f"INDETERMINATE / SEARCH_LIMIT: {exc}", file=sys.stderr)
        return 3
    except PermanentStartupError as exc:
        print(json.dumps(exc.finding, sort_keys=True), file=sys.stderr)
        return PERMANENT_EXIT
    except OSError as exc:
        if getattr(args, "managed_startup", False) and permanent_os_error(exc):
            finding = startup_finding("STARTUP_LOCAL_RESOURCE_ERROR", args.command, str(exc), errno=exc.errno)
            print(json.dumps(finding, sort_keys=True), file=sys.stderr)
            return PERMANENT_EXIT
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (InputError, CspfNoPath, SouthboundError, ValueError) as exc:
        if getattr(args, "managed_startup", False):
            finding = startup_finding("STARTUP_CONFIGURATION_INVALID", args.command, str(exc))
            print(json.dumps(finding, sort_keys=True), file=sys.stderr)
            return PERMANENT_EXIT
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
