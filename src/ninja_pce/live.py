"""Operator commands for live topology observation and passive PCEP sessions."""
from __future__ import annotations

import argparse
import asyncio
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

    pcep = subparsers.add_parser("pcep-listen", help="Run a passive lab PCEP listener; never sends LSP updates")
    pcep.add_argument("--listen-host", default="127.0.0.1", help="Bind address; select the lab NIC explicitly")
    pcep.add_argument("--port", type=int, default=4189)
    pcep.add_argument("--allow-peer", action="append", required=True, help="Allowed PCC source IP; repeatable")
    pcep.add_argument("--capture", required=True, help="Atomic session and LSP-report capture JSON")
    pcep.add_argument("--keepalive", type=int, default=30)
    pcep.add_argument("--deadtimer", type=int, default=120)


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

    server = PcepServer(
        PcepServerConfig(
            listen_host=args.listen_host, port=args.port, allowed_peers=tuple(args.allow_peer),
            keepalive=args.keepalive, deadtimer=args.deadtimer,
        ),
        capture_path=Path(args.capture),
    )
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await server.start()
        print(json.dumps({"mode": "passive_stateful", "listeners": server.addresses}), flush=True)
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
