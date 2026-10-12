"""Operator commands for live topology and opt-in lab PCEP computation."""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .state import write_state


def add_live_commands(subparsers: Any) -> None:
    bgpls = subparsers.add_parser("bgpls-collect", help="Collect live BGP-LS from a local GoBGP speaker")
    bgpls.add_argument("--endpoint", default="127.0.0.1:50051", help="Local GoBGP gRPC endpoint")
    bgpls.add_argument("--grpcurl", default="grpcurl", help="grpcurl executable")
    bgpls.add_argument("--proto", required=True, help="Read-only GoBGP API protobuf file")
    bgpls.add_argument("--peer", action="append", required=True, help="Required BGP-LS exporter IP; repeatable")
    bgpls.add_argument("--node-map", help="JSON object mapping advertised IPv4 router IDs to node names")
    bgpls.add_argument("--output", required=True, help="Normalized topology JSON file")
    bgpls.add_argument("--status", required=True, help="Collector health JSON file")
    bgpls.add_argument("--raw", help="Optional raw binary-NLRI capture JSON file")
    bgpls.add_argument("--interval", type=float, default=5.0)
    bgpls.add_argument("--timeout", type=float, default=10.0)
    bgpls.add_argument("--max-age", type=float, default=30.0)
    bgpls.add_argument("--once", action="store_true", help="Poll once and exit")

    pcep = subparsers.add_parser("pcep-listen", help="Observe lab PCEP; enable computation with explicit options")
    pcep.add_argument("--listen-host", default="127.0.0.1", help="Bind address; select the lab NIC explicitly")
    pcep.add_argument("--port", type=int, default=4189)
    pcep.add_argument("--allow-peer", action="append", required=True, help="Allowed PCC source IP; repeatable")
    pcep.add_argument("--capture", required=True, help="Atomic session and LSP-report capture JSON")
    pcep.add_argument("--keepalive", type=int, default=30)
    pcep.add_argument("--deadtimer", type=int, default=120)
    pcep.add_argument("--stateless-peer", action="append", default=[],
                      help="Omit stateful capability for this allowed PCC; repeatable")
    pcep.add_argument("--compute-bgpls-raw", help="Enable PCReq replies using this live raw BGP-LS capture")
    pcep.add_argument("--compute-bgpls-status", help="Live collector checksummed health file")
    pcep.add_argument("--compute-topology", help="Live normalized topology feed; must remain available")
    pcep.add_argument("--compute-bgpls-peer", action="append", default=[], help="Required live BGP-LS exporter")
    pcep.add_argument("--compute-node-map", help="Advertised router-ID to node-name JSON map")
    pcep.add_argument("--compute-headend", action="append", default=[],
                      help="Authorized PCC-IP=source-loopback; repeatable")
    pcep.add_argument("--compute-delegated-tunnel", action="append", default=[],
                      help="Opt in to initial delegated path provision for PCC-IP=tunnel-ID; repeatable")
    pcep.add_argument("--failover-tunnel", help="Opt in to recovery of one delegated PCC-IP=tunnel-ID")
    pcep.add_argument("--failover-state", help="Persistent failure-recovery journal; required with --failover-tunnel")
    pcep.add_argument("--creation-state", help="Opt in to private lab creation with a durable state directory")
    pcep.add_argument("--creation-socket", help="Owner-only Unix console socket; required for lab creation")
    pcep.add_argument("--creation-headend", action="append", default=[], help="Authorized node-name=PCC-IP")
    pcep.add_argument("--creation-destination", action="append", default=[], help="Authorized destination node name")
    pcep.add_argument("--creation-max-bandwidth", type=int, default=1_000_000, help="Lab creation cap in bits/s")


def _positive(value: float, label: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


def _node_names(path: str | None) -> dict[str, str]:
    if path is None:
        return {}
    from .loaders import _load_mapping

    names = _load_mapping(path)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()):
        raise ValueError("Node map must map router-ID strings to node-name strings")
    return names


def _write_document(path: Path, document: dict[str, Any]) -> None:
    """Publish loader-compatible JSON without the controller-state envelope."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as handle:
            temporary = handle.name
            json.dump(document, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _stop_collector(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def collect_bgpls(args: argparse.Namespace) -> int:
    from .bgpls import GoBgpTopologyProvider, parse_gobgp_snapshot, topology_document

    for value, label in ((args.interval, "interval"), (args.timeout, "timeout"), (args.max_age, "max-age")):
        _positive(value, label)
    names = _node_names(args.node_map)
    peers = tuple(args.peer)
    provider = GoBgpTopologyProvider(
        proto_path=Path(args.proto), endpoint=args.endpoint, grpcurl=args.grpcurl,
        required_peers=peers, node_names=names, timeout_seconds=args.timeout,
        max_age_seconds=args.max_age,
    )
    output, status = Path(args.output), Path(args.status)
    paths = [output, status] + ([Path(args.raw)] if args.raw else [])
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("Topology, status and raw capture paths must be distinct")
    previous_sigterm = signal.signal(signal.SIGTERM, _stop_collector)
    try:
        while True:
            observed = time.time()
            try:
                capture = provider.capture()
                if args.raw:
                    _write_document(Path(args.raw), capture)
                topology = parse_gobgp_snapshot(
                    capture, node_names=names, required_peers=peers, max_age_seconds=args.max_age,
                )
                if not topology.nodes or not topology.links:
                    raise ValueError("BGP-LS has no computable TE graph; inspect the raw capture and excluded objects")
                document = topology_document(topology)
                # The existing file-fed engine consumes this atomic live snapshot.
                _write_document(output, document)
                healthy = {
                    "status": "ok", "observed_at": observed, "nodes": len(topology.nodes),
                    "directed_links": len(topology.links), "metadata": topology.raw_metadata,
                }
                write_state(status, healthy)
                print(json.dumps(healthy, sort_keys=True), flush=True)
            except (ValueError, OSError, RuntimeError) as exc:
                # Never leave a last-good topology available as a current feed after
                # a known failed poll. Independent captures retain the evidence.
                output.unlink(missing_ok=True)
                failure = {"status": "unavailable", "observed_at": observed, "error": str(exc)}
                write_state(status, failure)
                print(json.dumps(failure, sort_keys=True), file=sys.stderr, flush=True)
                if args.once:
                    return 1
            else:
                if args.once:
                    return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        if not args.once:
            output.unlink(missing_ok=True)
            write_state(status, {"status": "stopped", "observed_at": time.time()})


async def _serve_pcep(args: argparse.Namespace) -> int:
    from .pcep import PcepServer, PcepServerConfig

    computation_args = [args.compute_bgpls_raw, args.compute_bgpls_status, args.compute_topology,
                        args.compute_bgpls_peer, args.compute_node_map, args.compute_headend]
    computer = None
    if any(computation_args):
        if not all(computation_args):
            raise ValueError("Computation requires raw, status, topology, exporter, node map and headend options")
        from .pcep_compute import PcepPathComputer

        headends: dict[str, str] = {}
        for binding in args.compute_headend:
            if binding.count("=") != 1:
                raise ValueError("Headend binding must be PCC-IP=source-loopback")
            peer, source = (str(ipaddress.IPv4Address(value)) for value in binding.split("="))
            if peer not in args.allow_peer or peer in headends:
                raise ValueError("Headends must be distinct allowed PCCs")
            headends[peer] = source
        paths = [Path(value).resolve() for value in
                 (args.capture, args.compute_bgpls_raw, args.compute_bgpls_status, args.compute_topology,
                  args.compute_node_map)]
        if len(set(paths)) != len(paths):
            raise ValueError("PCEP capture, raw, status, topology and node-map paths must be distinct")
        computer = PcepPathComputer(
            raw_path=Path(args.compute_bgpls_raw), status_path=Path(args.compute_bgpls_status),
            topology_path=Path(args.compute_topology), required_peers=tuple(args.compute_bgpls_peer),
            node_names=_node_names(args.compute_node_map), headends=headends,
        )
    server_options: dict[str, Any] = {"capture_path": Path(args.capture)}
    if computer is not None:
        server_options["path_computer"] = computer
    if args.compute_delegated_tunnel:
        if computer is None:
            raise ValueError("Delegated path provision requires all computation options")
        from .pcep_delegation import DelegatedPathComputer

        allowed_tunnels: dict[str, tuple[int, ...]] = {}
        for binding in args.compute_delegated_tunnel:
            if binding.count("=") != 1:
                raise ValueError("Delegated tunnel binding must be PCC-IP=tunnel-ID")
            address, tunnel = binding.split("=")
            peer = str(ipaddress.IPv4Address(address))
            if not tunnel.isascii() or not tunnel.isdecimal():
                raise ValueError("Delegated tunnel ID must be decimal")
            allowed_tunnels[peer] = (*allowed_tunnels.get(peer, ()), int(tunnel))
        server_options["delegated_computer"] = DelegatedPathComputer(computer, allowed_tunnels)
    if bool(args.failover_tunnel) != bool(args.failover_state):
        raise ValueError("Failure recovery requires both --failover-tunnel and --failover-state")
    if args.failover_tunnel:
        delegated = server_options.get("delegated_computer")
        if computer is None or delegated is None:
            raise ValueError("Failure recovery requires computation and explicit delegated tunnel authorization")
        if args.failover_tunnel.count("=") != 1:
            raise ValueError("Failure recovery binding must be PCC-IP=tunnel-ID")
        address, tunnel = args.failover_tunnel.split("=")
        peer = str(ipaddress.IPv4Address(address))
        if not tunnel.isascii() or not tunnel.isdecimal():
            raise ValueError("Failure recovery tunnel ID must be decimal")
        tunnel_id = int(tunnel)
        if tunnel_id not in delegated.allowed_tunnels.get(peer, ()):
            raise ValueError("Failure recovery tunnel must already be explicitly authorized for delegation")
        journal = Path(args.failover_state).resolve()
        if journal in paths:
            raise ValueError("Failure recovery journal must be distinct from capture and live input files")
        from .pcep_failover import FailureRecoveryPlanner

        server_options["failover"] = FailureRecoveryPlanner(
            computer, peer=peer, tunnel_id=tunnel_id, journal_path=journal,
        )
    creation_args = [args.creation_state, args.creation_socket, args.creation_headend, args.creation_destination]
    if any(creation_args):
        if not all(creation_args) or computer is None:
            raise ValueError("Private creation requires state, socket, endpoints and live computation options")
        from .pcep_creation import CreationSession
        from .tunnel_creation import CreationManager

        creation_headends = {}
        for binding in args.creation_headend:
            if binding.count("=") != 1:
                raise ValueError("Creation headend binding must be node-name=PCC-IP")
            name, address = binding.split("=")
            if name in creation_headends:
                raise ValueError("Creation headends must be distinct")
            creation_headends[name] = str(ipaddress.IPv4Address(address))
        server_options["creation"] = CreationSession(CreationManager(
            computer, Path(args.creation_state), creation_headends, tuple(args.creation_destination),
            max_bandwidth_bps=args.creation_max_bandwidth,
        ), Path(args.creation_socket))
    server = PcepServer(
        PcepServerConfig(
            listen_host=args.listen_host, port=args.port, allowed_peers=tuple(args.allow_peer),
            keepalive=args.keepalive, deadtimer=args.deadtimer,
            stateless_peers=tuple(args.stateless_peer),
        ),
        **server_options,
    )
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await server.start()
        print(json.dumps({"mode": "request_computation" if computer else "passive_stateful",
                          "listeners": server.addresses}), flush=True)
        await server.serve_forever()
    except asyncio.CancelledError:
        return 0
    finally:
        await server.close()
        loop.remove_signal_handler(signal.SIGTERM)
    return 0


def listen_pcep(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_serve_pcep(args))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        raise ValueError(f"Cannot run PCEP listener: {exc}") from exc
