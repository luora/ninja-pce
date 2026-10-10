"""Bounded, passive lab PCEP listener (RFC 5440 and RFC 8231).

This module observes RSVP LSP reports. It never sends PCUpd, PCInitiate,
delegation confirmations, or computed paths. Captures are evidence, not a TED
or reservation confirmation. Plain TCP provides no cryptographic peer identity.
"""
from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import math
import struct
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import load_state, write_state

OPEN, KEEPALIVE, PCREQ, PCREP, NOTIFICATION, ERROR, CLOSE = range(1, 8)
PCRPT = 10
MESSAGE_NAMES = {
    OPEN: "OPEN", KEEPALIVE: "KEEPALIVE", PCREQ: "PCReq", PCREP: "PCRep",
    NOTIFICATION: "PCNtf", ERROR: "PCErr", CLOSE: "CLOSE", PCRPT: "PCRpt",
}


class PcepProtocolError(ValueError):
    """A bounded parser rejected a message; optional wire error identifies why."""

    def __init__(self, message: str, error_type: int = 1, error_value: int = 1):
        super().__init__(message)
        self.error_type = error_type
        self.error_value = error_value


@dataclass(frozen=True)
class PcepObject:
    object_class: int
    object_type: int
    flags: int
    body: bytes


def encode_object(object_class: int, body: bytes, object_type: int = 1) -> bytes:
    length = len(body) + 4
    if not 1 <= object_class <= 255 or not 1 <= object_type <= 15 or length % 4 or length > 65528:
        raise ValueError("Invalid PCEP object encoding")
    return struct.pack("!BBH", object_class, object_type << 4, length) + body


def encode_message(message_type: int, body: bytes = b"") -> bytes:
    length = len(body) + 4
    if not 1 <= message_type <= 255 or length % 4 or length > 65532:
        raise ValueError("Invalid PCEP message encoding")
    return struct.pack("!BBH", 0x20, message_type, length) + body


def encode_open(keepalive: int = 30, deadtimer: int = 120, sid: int = 0, *, stateful: bool = True) -> bytes:
    for value in (keepalive, deadtimer, sid):
        if type(value) is not int or not 0 <= value <= 255:
            raise ValueError("OPEN fields must be unsigned octets")
    # TLV 16, U=0: passive stateful only. All extension capability flags clear.
    capability = struct.pack("!HHI", 16, 4, 0) if stateful else b""
    return encode_message(OPEN, encode_object(1, bytes((0x20, keepalive, deadtimer, sid)) + capability))


def encode_error(error_type: int, error_value: int) -> bytes:
    return encode_message(ERROR, encode_object(13, bytes((0, 0, error_type, error_value))))


def encode_close(reason: int = 1) -> bytes:
    return encode_message(CLOSE, encode_object(15, bytes((0, 0, 0, reason))))


def parse_objects(body: bytes) -> tuple[PcepObject, ...]:
    objects: list[PcepObject] = []
    offset = 0
    while offset < len(body):
        if len(body) - offset < 4:
            raise PcepProtocolError("Truncated object header")
        object_class, flags, length = struct.unpack_from("!BBH", body, offset)
        if length < 4 or length % 4 or offset + length > len(body):
            raise PcepProtocolError("Invalid object length")
        object_type = flags >> 4
        if object_class == 0 or object_type == 0:
            raise PcepProtocolError("Reserved object class or type", 3, 2 if object_type == 0 else 1)
        objects.append(PcepObject(object_class, object_type, flags & 0xF, body[offset + 4:offset + length]))
        offset += length
    return tuple(objects)


def parse_tlvs(body: bytes) -> tuple[tuple[int, bytes], ...]:
    tlvs: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(body):
        if len(body) - offset < 4:
            raise PcepProtocolError("Truncated TLV header")
        kind, length = struct.unpack_from("!HH", body, offset)
        end = offset + 4 + length
        padded_end = end + (-length % 4)
        if padded_end > len(body):
            raise PcepProtocolError("TLV length exceeds object")
        # RFC 5440: padding is not included in the TLV value length.
        tlvs.append((kind, body[offset + 4:end]))
        offset = padded_end
    return tuple(tlvs)


def parse_open(body: bytes) -> dict[str, Any]:
    objects = parse_objects(body)
    if len(objects) != 1 or (objects[0].object_class, objects[0].object_type) != (1, 1):
        raise PcepProtocolError("OPEN must contain exactly one OPEN object")
    content = objects[0].body
    if len(content) < 4 or content[0] >> 5 != 1:
        raise PcepProtocolError("Invalid OPEN version or length")
    tlvs = parse_tlvs(content[4:])
    capabilities = [value for kind, value in tlvs if kind == 16]
    if len(capabilities) > 1 or any(len(value) != 4 for value in capabilities):
        raise PcepProtocolError("Invalid STATEFUL-PCE-CAPABILITY TLV")
    keepalive, deadtimer, sid = content[1:4]
    if keepalive and deadtimer <= keepalive:
        raise PcepProtocolError("Peer DeadTimer must exceed its Keepalive", 1, 3)
    return {
        "keepalive": keepalive, "deadtimer": deadtimer if keepalive else 0, "sid": sid,
        "stateful": bool(capabilities),
        "stateful_flags": int.from_bytes(capabilities[0], "big") if capabilities else None,
        "tlvs": [{"type": kind, "value_hex": value.hex()} for kind, value in tlvs],
    }


def _route(body: bytes, *, recorded: bool = False) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while offset < len(body):
        if len(body) - offset < 2:
            raise PcepProtocolError("Truncated route subobject")
        kind, length = body[offset:offset + 2]
        if length < 2 or offset + length > len(body):
            raise PcepProtocolError("Invalid route subobject length")
        content = body[offset + 2:offset + length]
        sub_type = kind if recorded else kind & 0x7F
        item: dict[str, Any] = {"type": sub_type, "raw_hex": body[offset:offset + length].hex()}
        if not recorded:
            item["loose"] = bool(kind & 0x80)
        if sub_type in (1, 2):
            address_size = 4 if sub_type == 1 else 16
            if length != address_size + 4 or content[address_size] > address_size * 8:
                raise PcepProtocolError("Invalid IP route subobject")
            item.update(address=str(ipaddress.ip_address(content[:address_size])),
                        prefix_length=content[address_size])
        result.append(item)
        offset += length
    return result


def _lsp(obj: PcepObject) -> dict[str, Any]:
    if obj.object_type != 1 or len(obj.body) < 4:
        raise PcepProtocolError("Invalid LSP object")
    bits = int.from_bytes(obj.body[:4], "big")
    tlvs = parse_tlvs(obj.body[4:])
    data: dict[str, Any] = {
        "plsp_id": bits >> 12, "delegate": bool(bits & 1), "sync": bool(bits & 2),
        "removed": bool(bits & 4), "administrative_up": bool(bits & 8),
        "operational_code": (bits >> 4) & 7,
        "tlvs": [{"type": kind, "value_hex": value.hex()} for kind, value in tlvs],
    }
    data["operational_state"] = {
        0: "DOWN", 1: "UP", 2: "ACTIVE", 3: "GOING_DOWN", 4: "GOING_UP",
    }.get(data["operational_code"], "UNKNOWN")
    identifiers: list[dict[str, Any]] = []
    for kind, value in tlvs:
        if kind in (18, 19):
            address_size = 4 if kind == 18 else 16
            if len(value) != address_size * 3 + 4:
                raise PcepProtocolError("Invalid LSP-IDENTIFIERS TLV length")
            identifiers.append({
                "type": kind, "sender": str(ipaddress.ip_address(value[:address_size])),
                "lsp_id": int.from_bytes(value[address_size:address_size + 2], "big"),
                "tunnel_id": int.from_bytes(value[address_size + 2:address_size + 4], "big"),
                "extended_tunnel_id_hex": value[address_size + 4:2 * address_size + 4].hex(),
                "endpoint": str(ipaddress.ip_address(value[2 * address_size + 4:])),
                "all_zero": not any(value), "value_hex": value.hex(),
            })
        elif kind == 17:
            if not value or "symbolic_name_hex" in data:
                raise PcepProtocolError("Invalid or duplicate symbolic path name")
            data["symbolic_name_hex"] = value.hex()
            data["symbolic_name"] = value.decode("utf-8", errors="replace")
        elif kind == 20 and len(value) != 4:
            raise PcepProtocolError("Invalid LSP error-code TLV")
    if len(identifiers) > 1:
        raise PcepProtocolError("Duplicate LSP identifiers")
    data["identifiers"] = identifiers[0] if identifiers else None
    if data["plsp_id"] and not identifiers:
        raise PcepProtocolError("RSVP LSP report missing LSP-IDENTIFIERS TLV", 6, 11)
    if data["plsp_id"] == 0xFFFFF:
        raise PcepProtocolError("Reserved PLSP-ID")
    return data


def parse_reports(body: bytes) -> list[dict[str, Any]]:
    """Parse an entire PCRpt atomically; do not partially publish bad messages."""
    objects = parse_objects(body)
    reports: list[dict[str, Any]] = []
    index = 0
    while index < len(objects):
        srp_id = 0
        if objects[index].object_class == 33:
            srp = objects[index]
            if srp.object_type != 1 or len(srp.body) < 8:
                raise PcepProtocolError("Invalid SRP object")
            # RFC 8786: unsupported SRP flags ignored on receipt.
            srp_id = int.from_bytes(srp.body[4:8], "big")
            parse_tlvs(srp.body[8:])
            index += 1
        if index >= len(objects) or objects[index].object_class != 32:
            raise PcepProtocolError("PCRpt missing LSP object", 6, 8)
        report = _lsp(objects[index])
        report["srp_id"] = srp_id
        index += 1
        if index >= len(objects) or (objects[index].object_class, objects[index].object_type) != (7, 1):
            raise PcepProtocolError("PCRpt missing intended ERO", 6, 9)
        report["ero"] = _route(objects[index].body)
        index += 1
        attributes: list[dict[str, Any]] = []
        while index < len(objects) and objects[index].object_class not in (32, 33):
            obj = objects[index]
            attribute: dict[str, Any] = {"class": obj.object_class, "type": obj.object_type,
                                         "body_hex": obj.body.hex()}
            if obj.object_class == 8 and obj.object_type == 1:
                attribute["route"] = _route(obj.body, recorded=True)
                report["rro"] = attribute["route"]
            elif obj.object_class == 5 and obj.object_type in (1, 2):
                if len(obj.body) != 4:
                    raise PcepProtocolError("Invalid BANDWIDTH object")
                bandwidth = struct.unpack("!f", obj.body)[0]
                if not math.isfinite(bandwidth) or bandwidth < 0:
                    raise PcepProtocolError("Invalid BANDWIDTH value")
                attribute["bytes_per_second"] = bandwidth
                attribute["bits_per_second"] = bandwidth * 8
            attributes.append(attribute)
            index += 1
        report["attributes"] = attributes
        if report["delegate"]:
            raise PcepProtocolError("Delegation not negotiated: passive stateful only", 19, 1)
        if report["plsp_id"] == 0:
            if report["sync"] or report["removed"] or report["ero"]:
                raise PcepProtocolError("Invalid end-of-synchronization marker")
            if report["identifiers"] and not report["identifiers"]["all_zero"]:
                raise PcepProtocolError("Nonzero identifiers on sync marker")
            report["end_of_sync"] = True
        else:
            report["end_of_sync"] = False
        reports.append(report)
    if not reports:
        raise PcepProtocolError("Empty PCRpt", 6, 8)
    return reports


@dataclass(frozen=True)
class PcepServerConfig:
    allowed_peers: tuple[str, ...]
    listen_host: str = "127.0.0.1"
    port: int = 4189
    keepalive: int = 30
    deadtimer: int = 120
    handshake_timeout: float = 30.0
    max_message_bytes: int = 65532
    max_sessions: int = 16
    max_reports_per_peer: int = 1024
    max_report_bytes_per_peer: int = 2_097_152
    max_capture_events: int = 128
    max_capture_bytes: int = 1_048_576
    max_messages_per_minute: int = 600
    max_unknown_messages: int = 5
    observation_ttl: float = 120.0

    def __post_init__(self) -> None:
        ipaddress.ip_address(self.listen_host)
        if not isinstance(self.allowed_peers, tuple) or not self.allowed_peers or len(self.allowed_peers) > 256:
            raise ValueError("Explicit nonempty tuple of allowed peer IP addresses required (maximum256)")
        normalized = []
        for peer in self.allowed_peers:
            address = ipaddress.ip_address(peer)
            if address.is_unspecified or address.is_multicast:
                raise ValueError("Allowlist peers must be individual unicast IP addresses")
            normalized.append(str(address))
        if len(set(normalized)) != len(normalized):
            raise ValueError("Duplicate allowed peers")
        object.__setattr__(self, "allowed_peers", tuple(normalized))
        for field, low, high in (
            ("port", 0, 65535), ("keepalive", 0, 255), ("deadtimer", 0, 255),
            ("max_message_bytes", 20, 65532), ("max_sessions", 1, 256),
            ("max_reports_per_peer", 1, 4096), ("max_capture_events", 1, 1024),
            ("max_report_bytes_per_peer", 1024, 16_777_216), ("max_capture_bytes", 1024, 16_777_216),
            ("max_messages_per_minute", 1, 10000), ("max_unknown_messages", 1, 100),
        ):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid {field}")
        if self.max_message_bytes % 4:
            raise ValueError("max_message_bytes must be a multiple of4")
        if self.keepalive and self.deadtimer <= self.keepalive:
            raise ValueError("deadtimer must exceed keepalive")
        if not self.keepalive and self.deadtimer:
            raise ValueError("deadtimer must be0 when keepalive is disabled")
        for field in ("handshake_timeout", "observation_ttl"):
            value = getattr(self, field)
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(f"Invalid {field}")


@dataclass
class _Connection:
    writer: asyncio.StreamWriter
    state: dict[str, Any]
    last_rx: float
    last_tx: float
    remote_deadtimer: int = 0


class PcepServer:
    """Async TCP server. A capture path stores a bounded checksummed JSON snapshot."""

    def __init__(self, config: PcepServerConfig, *, capture_path: Path | None = None):
        self.config = config
        self.capture_path = capture_path
        self._server: asyncio.Server | None = None
        self._connections: dict[str, _Connection] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._peers: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=config.max_capture_events)
        self._sid = 0
        self._run_id = str(uuid.uuid4())
        self._capture_error: str | None = None
        self._closing = False
        self._rejected = 0
        if capture_path is not None and capture_path.exists():
            previous = load_state(capture_path)
            if previous.get("kind") != "passive_pcep_capture":
                raise ValueError("Capture path contains unrelated controller state")
            for peer, data in previous.get("peers", {}).items():
                if peer in config.allowed_peers and isinstance(data, dict):
                    data.update(state="DISCONNECTED", fresh=False, synchronized=False,
                                stale_reason="process_restart")
                    self._peers[peer] = data

    @property
    def addresses(self) -> list[tuple[Any, ...]]:
        return [sock.getsockname() for sock in self._server.sockets or ()] if self._server else []

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        peers = copy.deepcopy(self._peers)
        monotonic = time.monotonic()
        for peer, data in peers.items():
            connection = self._connections.get(peer)
            ttl = min(connection.remote_deadtimer or self.config.observation_ttl,
                      self.config.observation_ttl) if connection else 0
            fresh = bool(connection and data["state"] == "ESTABLISHED" and
                         monotonic - connection.last_rx < ttl)
            data["fresh"] = fresh
            data["valid_until"] = data.get("last_received_at", now) + ttl if fresh else None
            for report in data.get("reports", {}).values():
                report["stale"] = not (fresh and data.get("synchronized", False))
        return {
            "kind": "passive_pcep_capture", "run_id": self._run_id, "generated_at": now,
            "listener_running": self._server is not None and self._server.is_serving(),
            "mode": "passive_stateful", "authoritative": False, "complete": False,
            "capabilities": {"stateful": True, "lsp_update": False, "lsp_initiation": False},
            "capture_error": self._capture_error, "rejected_connections": self._rejected,
            "peers": peers, "events": list(self._events),
        }

    def _persist(self) -> None:
        if self.capture_path is not None:
            try:
                write_state(self.capture_path, self.snapshot())
            except OSError as exc:
                self._capture_error = str(exc)
                raise

    def _event(self, peer: str, direction: str, raw: bytes, **details: Any) -> None:
        self._events.append({"timestamp": time.time(), "peer": peer, "direction": direction,
                             "message_type": raw[1] if len(raw) > 1 else None,
                             "message_name": MESSAGE_NAMES.get(raw[1], "UNKNOWN") if len(raw) > 1 else None,
                             "raw_hex": raw.hex(), **details})
        while self._events and sum(len(item["raw_hex"]) for item in self._events) > self.config.max_capture_bytes:
            self._events.popleft()
        self._persist()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("PCEP server already started")
        self._closing = False
        self._server = await asyncio.start_server(self._accept, self.config.listen_host, self.config.port,
                                                 limit=self.config.max_message_bytes + 4, backlog=32)
        try:
            self._persist()
        except OSError:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            raise

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for peer, connection in tuple(self._connections.items()):
            try:
                await self._send(peer, connection, encode_close())
            except (ConnectionError, OSError):
                pass
            connection.writer.close()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._persist()

    async def _send(self, peer: str, connection: _Connection, raw: bytes) -> None:
        connection.writer.write(raw)
        await asyncio.wait_for(connection.writer.drain(), timeout=self.config.handshake_timeout)
        connection.last_tx = time.monotonic()
        self._event(peer, "tx", raw)

    async def _read(self, reader: asyncio.StreamReader) -> bytes:
        header = await reader.readexactly(4)
        version_flags, _, length = struct.unpack("!BBH", header)
        if version_flags >> 5 != 1:
            raise PcepProtocolError("Unsupported PCEP version")
        if length < 4 or length % 4 or length > self.config.max_message_bytes:
            raise PcepProtocolError("Invalid or oversized PCEP message length")
        return header + await reader.readexactly(length - 4)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        address = writer.get_extra_info("peername")
        peer = str(ipaddress.ip_address(address[0])) if address else ""
        if (self._closing or self._capture_error is not None or peer not in self.config.allowed_peers or
                peer in self._connections or len(self._connections) >= self.config.max_sessions):
            self._rejected += 1
            # RFC 5440 errata2940 allows ephemeral PCC source ports. Identity is the IP.
            if peer in self._connections:
                writer.write(encode_error(9, 0))
            writer.close()
            await writer.wait_closed()
            self._tasks.discard(task)
            return
        self._sid = (self._sid + 1) & 0xFF
        state: dict[str, Any] = {
            "state": "OPEN_WAIT", "session_id": str(uuid.uuid4()), "local_sid": self._sid,
            "peer_ip": peer, "peer_port": address[1], "source": f"pcep:{peer}",
            "connected_at": time.time(), "fresh": False, "synchronized": False,
            "reports": {}, "lsp_names": {}, "stale_reason": "synchronization_incomplete",
        }
        connection = _Connection(writer, state, time.monotonic(), time.monotonic())
        self._connections[peer] = connection
        self._peers[peer] = state
        keepalive_task: asyncio.Task[None] | None = None
        reason = "transport_closed"
        try:
            await self._send(peer, connection, encode_open(self.config.keepalive, self.config.deadtimer, self._sid))
            raw = await asyncio.wait_for(self._read(reader), self.config.handshake_timeout)
            self._event(peer, "rx", raw)
            if raw[1] != OPEN:
                raise PcepProtocolError("Expected OPEN as first message")
            remote = parse_open(raw[4:])
            connection.remote_deadtimer = remote["deadtimer"]
            state.update(peer_open=remote, state="KEEP_WAIT")
            await self._send(peer, connection, encode_message(KEEPALIVE))
            try:
                raw = await asyncio.wait_for(self._read(reader), self.config.handshake_timeout)
            except asyncio.TimeoutError as exc:
                raise PcepProtocolError("KeepWait timer expired", 1, 7) from exc
            self._event(peer, "rx", raw)
            if raw != encode_message(KEEPALIVE) and not (raw[1] == KEEPALIVE and len(raw) == 4):
                raise PcepProtocolError("Expected KEEPALIVE acknowledging OPEN")
            state.update(state="ESTABLISHED", established_at=time.time(), last_received_at=time.time())
            connection.last_rx = time.monotonic()
            self._persist()
            keepalive_task = asyncio.create_task(self._keepalive(peer, connection))
            received: deque[float] = deque()
            unknown: deque[float] = deque()
            while not self._closing:
                remaining = (connection.remote_deadtimer - (time.monotonic() - connection.last_rx)
                             if connection.remote_deadtimer else None)
                try:
                    raw = await asyncio.wait_for(self._read(reader), remaining)
                except asyncio.TimeoutError:
                    reason = "deadtimer_expired"
                    await self._send(peer, connection, encode_close(2))
                    break
                connection.last_rx = time.monotonic()
                state["last_received_at"] = time.time()
                while received and received[0] < connection.last_rx - 60:
                    received.popleft()
                received.append(connection.last_rx)
                self._event(peer, "rx", raw)
                if len(received) > self.config.max_messages_per_minute:
                    reason = "message_rate_exceeded"
                    await self._send(peer, connection, encode_close(5))
                    break
                message_type = raw[1]
                if message_type == KEEPALIVE:
                    if len(raw) != 4:
                        raise PcepProtocolError("KEEPALIVE must have an empty body")
                elif message_type == CLOSE:
                    objects = parse_objects(raw[4:])
                    if not objects or (objects[0].object_class, objects[0].object_type) != (15, 1):
                        raise PcepProtocolError("CLOSE object missing")
                    if len(objects[0].body) < 4:
                        raise PcepProtocolError("Invalid CLOSE object")
                    parse_tlvs(objects[0].body[4:])
                    reason = f"peer_close:{objects[0].body[3]}"
                    break
                elif message_type == OPEN:
                    raise PcepProtocolError("Unexpected OPEN on established session", 9, 0)
                elif message_type == ERROR:
                    objects = parse_objects(raw[4:])
                    errors = [obj for obj in objects if (obj.object_class, obj.object_type) == (13, 1)]
                    if not errors or any(len(obj.body) < 4 for obj in errors):
                        raise PcepProtocolError("Invalid PCErr")
                    for obj in errors:
                        parse_tlvs(obj.body[4:])
                    state["last_peer_errors"] = [{"type": obj.body[2], "value": obj.body[3]} for obj in errors]
                elif message_type == PCRPT:
                    if not remote["stateful"]:
                        raise PcepProtocolError("Stateful reporting was not negotiated", 19, 5)
                    self._apply_reports(state, parse_reports(raw[4:]))
                elif message_type == NOTIFICATION:
                    notifications = [obj for obj in parse_objects(raw[4:])
                                     if (obj.object_class, obj.object_type) == (12, 1)]
                    if not notifications or any(len(obj.body) < 4 for obj in notifications):
                        raise PcepProtocolError("Invalid PCNtf")
                    for obj in notifications:
                        parse_tlvs(obj.body[4:])
                else:
                    # No PCReq computation, update or initiation capabilities implemented.
                    parse_objects(raw[4:])
                    await self._send(peer, connection, encode_error(2, 0))
                    while unknown and unknown[0] < connection.last_rx - 60:
                        unknown.popleft()
                    unknown.append(connection.last_rx)
                    if len(unknown) >= self.config.max_unknown_messages:
                        reason = "unknown_messages_exceeded"
                        await self._send(peer, connection, encode_close(5))
                        break
                self._persist()
        except asyncio.TimeoutError:
            reason = "openwait_expired"
            await self._safe_send(peer, connection, encode_error(1, 2))
        except PcepProtocolError as exc:
            reason = str(exc)
            await self._safe_send(peer, connection, encode_error(exc.error_type, exc.error_value))
            if state["state"] == "ESTABLISHED":
                await self._safe_send(peer, connection, encode_close(3))
        except asyncio.IncompleteReadError:
            reason = "transport_eof"
        except (OSError, ConnectionError) as exc:
            reason = f"transport_or_capture_error:{exc}"
        finally:
            if keepalive_task is not None:
                keepalive_task.cancel()
                await asyncio.gather(keepalive_task, return_exceptions=True)
            self._connections.pop(peer, None)
            state.update(state="DISCONNECTED", fresh=False, synchronized=False, disconnected_at=time.time(),
                         stale_reason="server_shutdown" if self._closing else reason)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            self._tasks.discard(task)
            try:
                self._persist()
            except OSError:
                pass

    async def _safe_send(self, peer: str, connection: _Connection, raw: bytes) -> None:
        try:
            await self._send(peer, connection, raw)
        except (OSError, ConnectionError, asyncio.TimeoutError):
            pass

    async def _keepalive(self, peer: str, connection: _Connection) -> None:
        if not self.config.keepalive:
            return
        try:
            while True:
                await asyncio.sleep(max(0.01, self.config.keepalive - (time.monotonic() - connection.last_tx)))
                if time.monotonic() - connection.last_tx >= self.config.keepalive:
                    await self._send(peer, connection, encode_message(KEEPALIVE))
        except (OSError, ConnectionError, asyncio.TimeoutError):
            connection.writer.close()

    def _apply_reports(self, state: dict[str, Any], reports: list[dict[str, Any]]) -> None:
        working = copy.deepcopy(state["reports"])
        names = copy.deepcopy(state["lsp_names"])
        synchronized = state["synchronized"]
        for report in reports:
            if report["end_of_sync"]:
                synchronized = True
                continue
            if report["sync"] and synchronized:
                raise PcepProtocolError("Unexpected synchronization after sync completion")
            if not report["sync"] and not synchronized:
                raise PcepProtocolError("Non-sync LSP report before synchronization completed")
            identifiers = report["identifiers"]
            key = f"{report['plsp_id']}:{identifiers['type']}:{identifiers['value_hex']}"
            plsp = str(report["plsp_id"])
            previous_name = names.get(plsp)
            if "symbolic_name_hex" not in report:
                if previous_name is None:
                    raise PcepProtocolError("Initial report missing symbolic path name")
                report.update(symbolic_name=previous_name["name"], symbolic_name_hex=previous_name["hex"])
            if previous_name and previous_name["hex"] != report["symbolic_name_hex"]:
                raise PcepProtocolError("PLSP-ID symbolic path name changed during session")
            if any(value["hex"] == report["symbolic_name_hex"] and other != plsp
                   for other, value in names.items()):
                raise PcepProtocolError("Symbolic path name maps to multiple PLSP-IDs")
            names[plsp] = {"name": report["symbolic_name"], "hex": report["symbolic_name_hex"]}
            if len(names) > self.config.max_reports_per_peer:
                raise PcepProtocolError("LSP name binding storage limit exceeded")
            if report["removed"]:
                if identifiers["all_zero"]:
                    working = {name: value for name, value in working.items()
                               if value["plsp_id"] != report["plsp_id"]}
                else:
                    working.pop(key, None)
                continue
            report.update(received_at=time.time(), source=state["source"], session_id=state["session_id"],
                          authoritative=False, delegation_accepted=False)
            working[key] = report
            if len(working) > self.config.max_reports_per_peer:
                raise PcepProtocolError("Report storage limit exceeded")
        if len(json.dumps({"reports": working, "lsp_names": names}, allow_nan=False).encode()) > \
                self.config.max_report_bytes_per_peer:
            raise PcepProtocolError("Report byte storage limit exceeded")
        state.update(reports=working, lsp_names=names, synchronized=synchronized,
                     stale_reason=None if synchronized else "synchronization_incomplete")
