"""Read-only, bounded projections of the controller's existing observation files."""
from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .bgpls import OspfMultiAreaPolicy, parse_ospf_multi_area_policy
from .reservations import RESERVATION_STATES
from .state import SCHEMA_VERSION, digest

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_NODES = 256
MAX_LINKS = 2048
MAX_PEERS = 64
MAX_LSPS = 512
MAX_EVENTS = 80


@dataclass(frozen=True)
class DashboardConfig:
    topology_path: Path = Path("build/live/topology.json")
    bgpls_status_path: Path = Path("build/live/bgpls-status.json")
    bgpls_raw_path: Path = Path("build/live/bgpls-raw.json")
    pcep_path: Path = Path("build/live/pcep.json")
    state_dir: Path = Path("build/state")
    host: str = "127.0.0.1"
    port: int = 8088
    max_age_seconds: float = 30.0
    refresh_seconds: float = 5.0
    demo: bool = False
    ospf_multi_area: OspfMultiAreaPolicy | None = None

    def __post_init__(self) -> None:
        if not self.host or not 0 <= self.port <= 65535:
            raise ValueError("Dashboard requires a bind host and port between 0 and 65535")
        for value in (self.max_age_seconds, self.refresh_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Dashboard freshness and refresh intervals must be finite and positive")
        if self.ospf_multi_area is not None:
            if not isinstance(self.ospf_multi_area, OspfMultiAreaPolicy):
                raise ValueError("Dashboard OSPF multi-area policy requires validated configuration")
            object.__setattr__(self, "ospf_multi_area",
                               parse_ospf_multi_area_policy(self.ospf_multi_area.document()))


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            return None
        return float(value)
    except OverflowError:
        return None


def _time(value: Any) -> datetime | None:
    try:
        numeric = _number(value)
        if numeric is not None:
            return datetime.fromtimestamp(numeric, timezone.utc)
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _iso(value: Any) -> str | None:
    stamp = _time(value)
    return stamp.isoformat() if stamp else None


def _text(value: Any, default: str = "") -> str:
    return value[:512] if isinstance(value, str) else default


def _list(value: Any, limit: int = MAX_LINKS) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Observation list is malformed or exceeds dashboard limits")
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Observation object is malformed")
    return value


def _read(path: Path, *, checksummed: bool = False) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Observation must be a regular file")
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Observation exceeds dashboard file size limit")
    data = _mapping(json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value))))
    if checksummed:
        checksum = data.pop("checksum", None)
        if type(data.get("version")) is not int or data["version"] != SCHEMA_VERSION or checksum != digest(data):
            raise ValueError("Observation checksum or schema is invalid")
    return data


def _source(identifier: str, label: str, data: dict[str, Any] | None, now: datetime,
            max_age: float, stamp: Any = None, error: str | None = None) -> dict[str, Any]:
    timestamp = _time(stamp)
    age = (now - timestamp).total_seconds() if timestamp else None
    if error:
        state = error
    elif data is None:
        state = "unavailable"
    elif age is None or age < 0:
        state = "invalid"
    else:
        state = "fresh" if age <= max_age else "stale"
    messages = {
        "fresh": "Current controller observation; forwarding is not independently verified here.",
        "stale": "Retained observation is stale; current state is unknown.",
        "unavailable": "Observation file is unavailable.",
        "invalid": "Observation is malformed, inconsistent or has an invalid timestamp.",
        "unconfigured": "No local reservation ledger is available.",
    }
    return {"id": identifier, "label": label, "state": state,
            "age_seconds": round(age, 3) if age is not None else None,
            "timestamp": timestamp.isoformat() if timestamp else None, "message": messages[state]}


class DashboardStore:
    def __init__(self, config: DashboardConfig):
        self.config = config

    @staticmethod
    def _load(path: Path, *, checksummed: bool = False) -> tuple[dict[str, Any] | None, str | None]:
        try:
            return _read(path, checksummed=checksummed), None
        except OSError:
            return None, "unavailable"
        except (ValueError, TypeError, RecursionError):
            return None, "invalid"

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        refresh_clock = now is None
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("Dashboard clock requires a timezone")
        if self.config.demo:
            return _demo_snapshot(now, self.config.refresh_seconds)
        result: dict[str, Any] = {
            "schema_version": 1, "generated_at": now.isoformat(), "mode": "live", "read_only": True,
            "refresh_seconds": self.config.refresh_seconds, "sources": [], "summary": {},
            "topology": {"name": "Controller topology", "nodes": [], "links": []},
            "peers": [], "lsps": [], "computations": [], "events": [],
            "reservations": {"configured": False, "state": "unconfigured", "holds": 0, "entries": []},
        }
        now = self._topology(result, now, refresh_clock=refresh_clock)
        result["generated_at"] = now.isoformat()
        self._pcep(result, now)
        self._ledger(result, now)
        result["events"] = sorted(result["events"], key=lambda item: item["timestamp"])[-MAX_EVENTS:]
        result["computations"] = sorted(result["computations"], key=lambda item: item["timestamp"],
                                        reverse=True)[:MAX_EVENTS]
        result["summary"] = {
            "nodes": len(result["topology"]["nodes"]), "directed_links": len(result["topology"]["links"]),
            "pcep_peers": sum(peer["protocol"] == "PCEP" for peer in result["peers"]),
            "active_lsps": sum(lsp["fresh"] and lsp["operational_state"] == "ACTIVE" for lsp in result["lsps"]),
            "computations": len(result["computations"]), "reservations": result["reservations"]["holds"],
        }
        return result

    def _topology(self, result: dict[str, Any], now: datetime, *, refresh_clock: bool = False) -> datetime:
        config = self.config
        for attempt in range(3):
            feed, feed_error = self._load(config.topology_path)
            status, status_error = self._load(config.bgpls_status_path, checksummed=True)
            raw, raw_error = self._load(config.bgpls_raw_path)
            metadata = feed.get("metadata", {}) if feed else {}
            if not isinstance(metadata, dict):
                metadata, feed_error = {}, "invalid"
            status_metadata = status.get("metadata", {}) if status else {}
            if not isinstance(status_metadata, dict):
                status_metadata, status_error = {}, "invalid"
            stamps = (metadata.get("timestamp"), status_metadata.get("timestamp"),
                      raw.get("captured_at") if raw else None)
            mixed_generation = all(_time(stamp) is not None for stamp in stamps) and not (
                stamps[0] == stamps[1] == stamps[2])
            if (attempt == 2 or feed_error or status_error or raw_error or not status or
                    status.get("status") != "ok" or not mixed_generation):
                break
            # The collector atomically replaces three files in succession. Retry
            # a mixed generation, never a cached bundle, with at most 50 ms delay.
            time.sleep(0.025)
        if refresh_clock:
            now = datetime.now(timezone.utc)
        topology_source = _source("topology", "Topology", feed, now, config.max_age_seconds,
                                  metadata.get("timestamp"), feed_error)
        bgpls_source = _source("bgpls", "BGP-LS collector", status, now, config.max_age_seconds,
                              status_metadata.get("timestamp"), status_error)
        observed = _time(status.get("observed_at")) if status else None
        if bgpls_source["state"] == "fresh" and (
                observed is None or not 0 <= (now - observed).total_seconds() <= config.max_age_seconds):
            bgpls_source.update(state="invalid", message="Collector observation time is invalid or expired.")
        consistent = bool(feed and status and raw and status.get("status") == "ok" and
                          status_metadata == metadata and metadata.get("timestamp") == raw.get("captured_at"))
        if status and status.get("status") != "ok":
            bgpls_source.update(state="unavailable", message="Collector reports unavailable or stopped.")
        if raw_error or not consistent:
            if bgpls_source["state"] == "fresh":
                bgpls_source.update(state="invalid", message="Collector files do not describe the same observation.")
        if feed:
            try:
                # Observation metadata can confirm configured identity authority;
                # it must never enable a router projection by itself. Apply this
                # check to retained observations as well as current ones.
                for item in (metadata, status_metadata):
                    if config.ospf_multi_area is None:
                        if "ospf_multi_area" in item or "ospf_multi_area_digest" in item:
                            raise ValueError("Collector OSPF area policy requires dashboard opt-in")
                    elif (item.get("ospf_multi_area") != config.ospf_multi_area.document() or
                          item.get("ospf_multi_area_digest") != config.ospf_multi_area.digest):
                        raise ValueError("Collector OSPF area policy differs from dashboard policy")
                nodes = _list(feed.get("nodes"), MAX_NODES)
                links = _list(feed.get("links"), MAX_LINKS)
                result["topology"] = {
                    "name": _text(feed.get("name"), _text(metadata.get("name"), "Live TE topology")),
                    "nodes": [{"id": _text(_mapping(node).get("id")), "loopback": _text(node.get("loopback")),
                               "area": _text(node.get("area"))} for node in nodes],
                    "links": [{"id": _text(_mapping(link).get("id")), "source": _text(link.get("from")),
                               "target": _text(link.get("to")), "metric": _number(link.get("metric")),
                               "reservable_bw_bps": _number(link.get("reservable_bw_bps")),
                               "available_bw_bps": _number((_list(link.get("unreserved_bw_bps"), 8) or [None])[-1]),
                               "reserved_bw_bps": _number(link.get("reserved_bw_bps")),
                               "local_address": _text(link.get("from_ip")),
                               "remote_address": _text(link.get("to_ip"))} for link in links],
                }
                if consistent and bgpls_source["state"] == "fresh":
                    from .bgpls import parse_gobgp_snapshot, topology_document

                    names = {node["loopback"]: node["id"] for node in result["topology"]["nodes"]}
                    required = tuple(_list(metadata.get("required_peers"), MAX_PEERS))
                    parsed = parse_gobgp_snapshot(raw or {}, node_names=names, required_peers=required,
                                                  now=now, max_age_seconds=config.max_age_seconds,
                                                  ospf_multi_area=config.ospf_multi_area)
                    if topology_document(parsed) != feed:
                        raise ValueError("Normalized topology differs from its captured BGP-LS input")
                    if status is None:
                        raise ValueError("Collector status is unavailable")
                    if (type(status.get("nodes")) is not int or status["nodes"] != len(parsed.nodes) or
                            type(status.get("directed_links")) is not int or
                            status["directed_links"] != len(parsed.links)):
                        raise ValueError("Collector counts differ from the normalized topology")
                    for address in required:
                        result["peers"].append({
                            "id": f"bgpls:{address}", "protocol": "BGP-LS", "address": address,
                            "state": "ESTABLISHED", "fresh": True, "synchronized": True,
                            "last_seen": bgpls_source["timestamp"], "capabilities": ["BGP-LS", "End-of-RIB"],
                            "reports": len(result["topology"]["links"]),
                        })
            except (ValueError, TypeError, KeyError, OverflowError):
                topology_source.update(state="invalid", message="Topology observation is malformed or inconsistent.")
                bgpls_source.update(state="invalid", message="BGP-LS input does not validate against the topology.")
                result["topology"] = {"name": "Controller topology", "nodes": [], "links": []}
        if bgpls_source["state"] != "fresh" and topology_source["state"] == "fresh":
            topology_source.update(state="stale", message="Collector consistency cannot establish current topology.")
        if not any(peer["protocol"] == "BGP-LS" for peer in result["peers"]):
            required = status_metadata.get("required_peers", [])
            if isinstance(required, list):
                for address in required[:MAX_PEERS]:
                    result["peers"].append({"id": f"bgpls:{_text(address)}", "protocol": "BGP-LS",
                                            "address": _text(address), "state": "UNKNOWN", "fresh": False,
                                            "synchronized": False, "last_seen": bgpls_source["timestamp"],
                                            "capabilities": ["BGP-LS"], "reports": 0})
        result["sources"].extend([topology_source, bgpls_source])
        return now

    def _pcep(self, result: dict[str, Any], now: datetime) -> None:
        capture, error = self._load(self.config.pcep_path, checksummed=True)
        source = _source("pcep", "PCEP listener", capture, now, self.config.max_age_seconds,
                         capture.get("generated_at") if capture else None, error)
        if capture:
            try:
                if capture.get("kind") != "passive_pcep_capture":
                    raise ValueError("Unrelated controller state")
                if capture.get("listener_running") is not True or capture.get("capture_error"):
                    source.update(state="unavailable", message="PCEP listener is stopped or cannot persist observations.")
                peers = _mapping(capture.get("peers"))
                if len(peers) > MAX_PEERS:
                    raise ValueError("Too many PCEP peers")
                for address, peer in peers.items():
                    self._peer(result, _text(address), _mapping(peer), source["state"] == "fresh", now)
                if peers and source["state"] == "fresh" and not any(
                        item["protocol"] == "PCEP" and item["fresh"] for item in result["peers"]):
                    source.update(state="stale", message="Capture retained; no PCC observation is currently fresh.")
                for event in _list(capture.get("events", []), 1024)[-MAX_EVENTS:]:
                    event = _mapping(event)
                    stamp = _iso(event.get("timestamp"))
                    if stamp is None:
                        continue
                    name = _text(event.get("message_name"), "PCEP event")
                    result["events"].append({
                        "id": f"event:{len(result['events'])}:{stamp}", "timestamp": stamp,
                        "peer": _text(event.get("peer")), "direction": _text(event.get("direction")),
                        "name": name, "detail": "Protocol message observed",
                        "level": "error" if name == "ERROR" else "warning" if name == "CLOSE" else "info",
                    })
            except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                source.update(state="invalid", message="PCEP observation schema is invalid.")
                result["peers"] = [peer for peer in result["peers"] if peer["protocol"] != "PCEP"]
                result["lsps"], result["computations"], result["events"] = [], [], []
        result["sources"].append(source)

    def _peer(self, result: dict[str, Any], address: str, peer: dict[str, Any], capture_fresh: bool,
              now: datetime) -> None:
        last = _time(peer.get("last_received_at"))
        deadline = _time(peer.get("valid_until"))
        # Capture age measures the observer's health. An idle synchronized PCC
        # stays current until the listener's session deadline, which may exceed
        # the generic file-age limit between otherwise healthy keepalives.
        fresh = bool(capture_fresh and peer.get("fresh") is True and peer.get("state") == "ESTABLISHED" and
                     last and last <= now and
                     deadline and now < deadline)
        synchronized = peer.get("synchronized") is True
        reports = _mapping(peer.get("reports", {}))
        if len(reports) > MAX_LSPS or len(result["lsps"]) + len(reports) > MAX_LSPS:
            raise ValueError("Too many observed LSPs")
        result["peers"].append({
            "id": f"pcep:{address}", "protocol": "PCEP", "address": address,
            "state": _text(peer.get("state"), "UNKNOWN") if fresh else "STALE",
            "fresh": fresh, "synchronized": synchronized and fresh,
            "last_seen": last.isoformat() if last else None,
            "capabilities": ["Stateful"] if peer.get("local_stateful") else [], "reports": len(reports),
        })
        current: dict[Any, tuple[str, dict[str, Any]]] = {}
        for key, value in reports.items():
            report = _mapping(value)
            if report.get("removed") is True or report.get("end_of_sync") is True:
                continue
            identity = report.get("plsp_id")
            previous = current.get(identity)
            if previous is None or (_number(report.get("received_at")) or 0) >= (
                    _number(previous[1].get("received_at")) or 0):
                current[identity] = (key, report)
        for key, report in current.values():
            ids = _mapping(report.get("identifiers", {}))
            attributes = _list(report.get("attributes", []), 512)
            bandwidth = next((_number(attr.get("bits_per_second")) for attr in attributes
                              if isinstance(attr, dict) and attr.get("class") == 5 and attr.get("type") == 1), None)
            lspa = next((attr for attr in attributes if isinstance(attr, dict) and
                         attr.get("class") == 9 and attr.get("type") == 1), {})
            body = bytes.fromhex(_text(lspa.get("body_hex")))
            live = fresh and synchronized and report.get("stale") is not True
            result["lsps"].append({
                "id": f"{address}:{_text(key)}", "name": _text(report.get("symbolic_name"), "Unnamed LSP"),
                "peer": address, "source": _text(ids.get("sender")), "destination": _text(ids.get("endpoint")),
                "tunnel_id": _number(ids.get("tunnel_id")), "plsp_id": _number(report.get("plsp_id")),
                "operational_state": _text(report.get("operational_state"), "UNKNOWN") if live else "STALE",
                "reported_state": _text(report.get("operational_state"), "UNKNOWN"), "fresh": live,
                "delegated": live and report.get("delegate") is True,
                "bandwidth_bps": bandwidth, "ero": self._route(report.get("ero", [])),
                "ero_hops": self._ero_hops(report.get("ero", [])),
                "rro": self._route(report.get("rro", [])),
                "setup_priority": body[12] if len(body) == 16 else None,
                "hold_priority": body[13] if len(body) == 16 else None,
            })
        computations = _mapping(peer.get("updates", {}))
        for key, update in list(computations.items())[-MAX_EVENTS:]:
            update = _mapping(update)
            self._computation(result, address, _text(key), update, update.get("sent_at"))
        latest = peer.get("last_computation")
        if isinstance(latest, dict):
            for index, outcome in enumerate(_list(latest.get("outcomes", []), MAX_EVENTS)):
                self._computation(result, address, f"request:{index}", _mapping(outcome), latest.get("timestamp"))

    @staticmethod
    def _route(value: Any) -> list[str]:
        return [_text(hop.get("address")) if isinstance(hop, dict) else _text(hop)
                for hop in _list(value, MAX_NODES)]

    @staticmethod
    def _ero_hops(value: Any) -> list[dict[str, Any]]:
        hops = []
        for item in _list(value, MAX_NODES):
            hop = item if isinstance(item, dict) else {}
            hops.append({"address": _text(hop.get("address"), _text(item)),
                         "type": hop.get("type") if type(hop.get("type")) is int else None,
                         "prefix_length": hop.get("prefix_length") if type(hop.get("prefix_length")) is int else None,
                         "loose": hop.get("loose") if type(hop.get("loose")) is bool else None})
        return hops

    @staticmethod
    def _computation(result: dict[str, Any], peer: str, key: str, update: dict[str, Any], stamp: Any) -> None:
        timestamp = _iso(stamp)
        if timestamp is None:
            return
        result["computations"].append({
            "id": f"{peer}:{key}", "timestamp": timestamp, "peer": peer,
            "status": _text(update.get("status"), "UNKNOWN"), "source": _text(update.get("source")),
            "destination": _text(update.get("destination")), "bandwidth_bps": _number(update.get("bandwidth_bps")),
            "path_nodes": [_text(node) for node in _list(update.get("path_nodes", []), MAX_NODES)],
            "ero": DashboardStore._route(update.get("ero", [])), "metric": _number(update.get("total_metric")),
            "reason": {"NO_PATH": "No admissible path was reported.",
                       "REJECTED": "Request constraints were rejected.",
                       "TOPOLOGY_UNAVAILABLE": "Fresh topology was unavailable.",
                       "SEARCH_LIMIT": "Computation search budget was exhausted.",
                       "DELEGATION_RETURNED": "Control was returned to the PCC.",
                       "ACK_TIMEOUT": "PCC acknowledgment timed out."}.get(_text(update.get("status"))),
            "acknowledged": update.get("acknowledged") is True,
            "reported_state": _text(update.get("latest_reported_state"), _text(update.get("reported_state"))),
        })

    def _ledger(self, result: dict[str, Any], now: datetime) -> None:
        data, error = self._load(self.config.state_dir / ".ninja-pce-reservations.json", checksummed=True)
        source = _source("ledger", "Local reservations", data, now, self.config.max_age_seconds,
                         error="unconfigured" if error == "unavailable" else error)
        if data:
            try:
                entries = _mapping(data.get("entries"))
                if len(entries) > MAX_LSPS:
                    raise ValueError("Too many ledger entries")
                projected = []
                for identity, entry in entries.items():
                    entry = _mapping(entry)
                    bandwidth = _number(entry.get("current_bandwidth_bps"))
                    if (not identity or not isinstance(entry.get("owner"), str) or not entry["owner"] or
                            entry.get("status") not in RESERVATION_STATES):
                        raise ValueError("Invalid ledger identity or state")
                    if type(entry.get("current_bandwidth_bps")) is not int or bandwidth is None or bandwidth <= 0:
                        raise ValueError("Invalid ledger bandwidth")
                    resources = _mapping(entry.get("resources"))
                    current_edges = _list(entry.get("current_edges"), MAX_LINKS)
                    if len(resources) > MAX_LINKS or not all(
                            isinstance(edge, str) and type(amount) is int and amount > 0 and
                            _number(amount) is not None for edge, amount in resources.items()):
                        raise ValueError("Invalid directional ledger resources")
                    if not all(isinstance(edge, str) and resources.get(edge, 0) >= bandwidth
                               for edge in current_edges):
                        raise ValueError("Current path exceeds held resources")
                    projected.append({"id": _text(identity), "status": entry["status"],
                                      "bandwidth_bps": bandwidth, "current_bandwidth_bps": bandwidth,
                                      "current_edges": current_edges, "edges": list(resources),
                                      "held_resources": resources})
                source.update(state="fresh", message="Local held-resource accounting; independent of PCEP observations.")
                result["reservations"] = {"configured": True, "state": "available", "holds": len(projected),
                                           "entries": projected}
            except (ValueError, TypeError, KeyError):
                source.update(state="invalid", message="Local reservation ledger schema is invalid.")
                result["reservations"]["state"] = "invalid"
        result["sources"].append(source)


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self._slots = threading.BoundedSemaphore(4)
        super().__init__(address, handler)

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def create_dashboard_server(config: DashboardConfig) -> ThreadingHTTPServer:
    store = DashboardStore(config)
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/index.html": ("index.html", "text/html; charset=utf-8"),
              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/styles.css": ("styles.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "NinjaPCE-Dashboard"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _respond(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if status == 405:
                self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in assets:
                name, content_type = assets[path]
                self._respond(200, files("ninja_pce").joinpath("web").joinpath(name).read_bytes(), content_type)
            elif path == "/api/controller":
                self._respond(200, json.dumps(store.snapshot(), allow_nan=False).encode())
            elif path == "/api/tunnel-options":
                self._respond(200, json.dumps({
                    "enabled": False, "headends": [], "destinations": [], "max_bandwidth_bps": 0,
                    "reason": ("Tunnel creation is disabled in the demo workspace." if config.demo else
                               "RSVP bandwidth creation has not been qualified for this controller."),
                    "policy": {"read_only": True},
                }).encode())
            elif path == "/healthz":
                self._respond(200, json.dumps({"status": "ok", "read_only": True,
                                              "mode": "demo" if config.demo else "live"}).encode())
            else:
                self._respond(404, b'{"error":"Not found"}')

        do_HEAD = do_GET

        def _reject(self) -> None:
            self._respond(405, b'{"error":"Dashboard is read-only"}')

        do_POST = _reject
        do_PUT = _reject
        do_PATCH = _reject
        do_DELETE = _reject
        do_OPTIONS = _reject
        do_TRACE = _reject
        do_CONNECT = _reject

    return _DashboardServer((config.host, config.port), Handler)


def _demo_snapshot(now: datetime, refresh: float) -> dict[str, Any]:
    """Synthetic examples only; never re-date or rewrite historical lab evidence."""
    from datetime import timedelta

    nodes = [{"id": name, "loopback": f"10.255.0.{number}", "area": "0"}
             for name, number in (("PE1", 1), ("P1", 2), ("P3", 3), ("P4", 4), ("P2", 5), ("PE3", 6))]
    pairs = [("PE1", "P1", "12", 1, 2), ("PE1", "P3", "13", 1, 3),
             ("P1", "P2", "25", 2, 5), ("P1", "P3", "23", 2, 3),
             ("P3", "P4", "34", 3, 4), ("P2", "P4", "54", 5, 4),
             ("P2", "PE3", "56", 5, 6), ("P4", "PE3", "46", 4, 6)]
    links = []
    active = {("PE1", "P3"), ("P3", "P4"), ("P4", "PE3")}
    for left, right, subnet, a, b in pairs:
        for source, target, local, remote in ((left, right, a, b), (right, left, b, a)):
            held = 50_000_000 if (source, target) in active else 0
            links.append({"id": f"demo:{source}:{target}", "source": source, "target": target, "metric": 10,
                          "reservable_bw_bps": 100_000_000, "available_bw_bps": 100_000_000 - held,
                          "reserved_bw_bps": held, "local_address": f"10.0.{subnet}.{local}",
                          "remote_address": f"10.0.{subnet}.{remote}"})
    stamp = now.isoformat()
    peers = [{"id": "bgpls:172.16.100.102", "protocol": "BGP-LS", "address": "172.16.100.102",
              "state": "ESTABLISHED", "fresh": True, "synchronized": True, "last_seen": stamp,
              "capabilities": ["BGP-LS", "End-of-RIB"], "reports": 16},
             {"id": "pcep:10.255.0.1", "protocol": "PCEP", "address": "10.255.0.1",
              "state": "ESTABLISHED", "fresh": True, "synchronized": True, "last_seen": stamp,
              "capabilities": ["Stateful", "Initial path update"], "reports": 2}]
    lsps = [{"id": "demo:Tunnel100", "name": "Tunnel100", "peer": "10.255.0.1",
             "source": "10.255.0.1", "destination": "10.255.0.6", "tunnel_id": 100, "plsp_id": 524388,
             "operational_state": "ACTIVE", "reported_state": "ACTIVE", "fresh": True, "delegated": True,
             "bandwidth_bps": 50_000_000, "ero": ["10.0.13.3", "10.0.34.4", "10.0.46.6"], "rro": [],
             "ero_hops": [{"type": 1, "prefix_length": 32, "loose": False, "address": address}
                          for address in ("10.0.13.3", "10.0.34.4", "10.0.46.6")],
             "setup_priority": 7, "hold_priority": 7},
            {"id": "demo:Tunnel200", "name": "Tunnel200", "peer": "10.255.0.1",
             "source": "10.255.0.1", "destination": "10.255.0.6", "tunnel_id": 200, "plsp_id": 524489,
             "operational_state": "DOWN", "reported_state": "DOWN", "fresh": True, "delegated": False,
             "bandwidth_bps": 120_000_000, "ero": [], "rro": [], "setup_priority": 7, "hold_priority": 7}]
    return {
        "schema_version": 1, "generated_at": stamp, "mode": "demo", "read_only": True,
        "refresh_seconds": refresh,
        "sources": [{"id": identifier, "label": label, "state": "fresh", "age_seconds": 0,
                     "timestamp": stamp, "message": "Synthetic demo data; no controller or routers connected."}
                    for identifier, label in (("topology", "Topology"), ("bgpls", "BGP-LS collector"),
                                              ("pcep", "PCEP listener"), ("ledger", "Local reservations"))],
        "summary": {"nodes": 6, "directed_links": 16, "pcep_peers": 1, "active_lsps": 1,
                    "computations": 2, "reservations": 0},
        "topology": {"name": "Six-router demonstration", "nodes": nodes, "links": links},
        "peers": peers, "lsps": lsps,
        "computations": [{"id": "demo:3", "timestamp": stamp, "peer": "10.255.0.1", "status": "INITIAL_PATH",
                          "source": "10.255.0.1", "destination": "10.255.0.6", "bandwidth_bps": 50_000_000,
                          "path_nodes": ["PE1", "P3", "P4", "PE3"], "ero": lsps[0]["ero"], "metric": 30,
                          "reason": "Synthetic initial path example", "acknowledged": True, "reported_state": "ACTIVE"},
                         {"id": "demo:4", "timestamp": stamp, "peer": "10.255.0.1", "status": "NO_PATH",
                          "source": "10.255.0.1", "destination": "10.255.0.6", "bandwidth_bps": 120_000_000,
                          "path_nodes": [], "ero": [], "metric": None, "reason": "Insufficient reservable bandwidth",
                          "acknowledged": False, "reported_state": "DOWN"}],
        "events": [{"id": f"demo:{index}", "timestamp": (now - timedelta(seconds=seconds)).isoformat(),
                    "peer": "10.255.0.1", "direction": direction, "name": name, "detail": detail, "level": level}
                   for index, (seconds, direction, name, detail, level) in enumerate([
                       (24, "rx", "OPEN", "Synthetic PCC session established", "info"),
                       (18, "rx", "PCRpt", "Tunnel100 requested initial delegated path", "info"),
                       (17, "tx", "PCUpd", "Computed PE1 → P3 → P4 → PE3, 50 Mbps", "info"),
                       (15, "rx", "PCRpt", "Tunnel100 reported ACTIVE; forwarding unverified here", "info"),
                       (8, "tx", "NO_PATH", "Tunnel200 requests 120 Mbps; no admissible path", "warning"),
                   ])],
        "reservations": {"configured": False, "state": "unconfigured", "holds": 0, "entries": []},
    }
