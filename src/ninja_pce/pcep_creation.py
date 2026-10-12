"""Private, explicitly enabled lab initiation on the existing PCEP writer.

The Unix socket is an operator console boundary, not a public HTTP API. It is
0600 and accepts only the process owner. Public dashboard creation remains
disabled until an exact platform profile has passed qualification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import stat
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cspf import CspfNoPath, CspfSearchLimit
from .pcep_initiation import encode_delete, encode_initiate, encode_owned_update, supports_rsvp_initiation
from .tunnel_creation import CreationManager

if TYPE_CHECKING:
    from .pcep import PcepServer, _Connection


def _enabled() -> bool:
    return os.environ.get("NINJA_PCE_PREVIEW_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate private control request field")
        result[key] = value
    return result


class CreationSession:
    """One bounded lab operation; no automatic retries or public deletion."""

    def __init__(self, manager: CreationManager, socket_path: Path):
        self.manager, self.path = manager, Path(socket_path)
        self.server: PcepServer | None = None
        self.listener: asyncio.Server | None = None
        self.monitor: asyncio.Task[None] | None = None
        self._control_lock = asyncio.Lock()

    def allows(self, peer: str) -> bool:
        return peer in self.manager.headends.values()

    def _failed(self, error: Exception) -> None:
        if self.server is not None:
            self.server._capture_error = f"Creation state unavailable: {type(error).__name__}: {error}"[:256]

    async def start(self, server: PcepServer) -> None:
        self.server = server
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = self.path.parent.stat()
        if (self.path.parent.is_symlink() or not stat.S_ISDIR(parent.st_mode) or
                parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise ValueError("Creation socket requires an owner-only directory")
        if self.path.exists() or self.path.is_symlink():
            raise ValueError("Creation control socket already exists; reconcile the previous process first")
        self.listener = await asyncio.start_unix_server(self._accept, path=str(self.path), limit=4097)
        self.path.chmod(0o600)
        # A restarted process never repeats an uncertain operation.
        for op in self.manager.list():
            if op["status"] in {"PUSHING", "APPLIED_UNVERIFIED"}:
                self.manager.uncertain(op["id"], "controller_restart_reconciliation_required")
        self.monitor = asyncio.create_task(self._watch())

    async def close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
            self.path.unlink(missing_ok=True)
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None

    def _connection(self, peer: str) -> _Connection:
        server = self.server
        connection = server._connections.get(peer) if server is not None else None
        if (server is None or server._capture_error is not None or not _enabled() or connection is None or
                not self.allows(peer) or not server._recovery_session_fresh(connection) or
                not connection.state.get("local_initiation") or
                not supports_rsvp_initiation(connection.state.get("peer_open", {}))):
            raise ValueError("Creation requires an enabled, fresh synchronized RSVP-initiation session")
        if server.failover is not None and server.failover.snapshot().get("pending"):
            raise ValueError("Failure recovery is pending on the shared writer")
        if connection.computation is not None and not connection.computation.done():
            raise ValueError("Another computation is pending on the shared writer")
        if any(not item["acknowledged"] for item in connection.state.get("updates", {}).values()):
            raise ValueError("Another update is awaiting acknowledgment on the shared writer")
        return connection

    async def create(self, principal: str, key: str, intent: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(intent, dict) or set(intent) != {"headend", "destination", "bandwidth_bps"}:
            raise ValueError("Creation requires exactly headend, destination and bandwidth_bps")
        # Serializes admission, idempotency and scheduling for the private API.
        async with self._control_lock:
            op = self.manager.plan(principal, key, **intent)
            if op["status"] != "PLANNED" or op["send"] is not None:
                return op
            try:
                connection = self._connection(op["peer"])
                assert self.server is not None
                async with connection.send_lock:
                    if self._connection(op["peer"]) is not connection:
                        raise ValueError("PCC connection changed before creation")
                    if connection.srp_sequence >= 0xFFFFFFFE:
                        raise ValueError("Session SRP identifiers are exhausted")
                    srp = connection.srp_sequence + 1
                    raw = encode_initiate(srp, op["symbolic_name"], op["source_ip"], op["destination_ip"],
                                          tuple(op["ero"]), op["bandwidth_bps"],
                                          op["setup_priority"], op["hold_priority"])
                    # mark_sending revalidates policy, expiry and fresh TED under
                    # the resource lock. Nothing awaits between it and write.
                    self.manager.mark_sending(op["id"], connection.state["session_id"], srp,
                                              hashlib.sha256(raw).hexdigest())
                    connection.srp_sequence = srp
                    connection.writer.write(raw)
                    await asyncio.wait_for(connection.writer.drain(), self.server.config.handshake_timeout)
                    connection.last_tx = time.monotonic()
                    self.server._event(op["peer"], "tx", raw, operation_id=op["id"])
            except asyncio.CancelledError:
                try:
                    current = self.manager.get(op["id"])
                    if current["send"] is not None:
                        self.manager.uncertain(op["id"], "creation_delivery_cancelled_reconciliation_required")
                    else:
                        self.manager.reject_before_send(op["id"], "creation_cancelled_before_send")
                except (ValueError, OSError) as exc:
                    self._failed(exc)
                raise
            except (ValueError, OSError, ConnectionError, asyncio.TimeoutError) as exc:
                current = self.manager.get(op["id"])
                reason = str(exc)[:128] or type(exc).__name__
                if current["send"] is None:
                    return self.manager.reject_before_send(op["id"], reason)
                return self.manager.uncertain(op["id"], reason)
            return self.manager.get(op["id"])

    def owns(self, peer: str, report: dict[str, Any]) -> bool:
        """Suppress the unrelated preconfigured-tunnel delegation responder."""
        try:
            for op in self.manager.list():
                send = op.get("send")
                bound = op.get("owned_candidate") or op.get("reported")
                known_identity = bool(bound and report.get("plsp_id") == bound["identity"]["plsp_id"] and
                                      report.get("identifiers") == bound["identity"]["identifiers"])
                if (op["peer"] == peer and send and report.get("session_id") == send["session_id"] and
                        report.get("symbolic_name_hex") == op["symbolic_name"].encode().hex()):
                    return True
                if (op["peer"] == peer and known_identity and
                        report.get("symbolic_name_hex") == op["symbolic_name"].encode().hex()):
                    return True  # Preserve known state through sync; do not adopt it.
            return False
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self._failed(exc)
            return True  # Unknown ownership must never invoke a different writer.

    def observe(self, peer: str, reports: list[dict[str, Any]]) -> None:
        try:
            for report in reports:
                if report["end_of_sync"] or report["sync"] or not self.owns(peer, report):
                    continue
                for op in self.manager.list():
                    if op["peer"] != peer or report.get("symbolic_name_hex") != op["symbolic_name"].encode().hex():
                        continue
                    if report["session_id"] != op.get("owner_session", op["send"]["session_id"]):
                        continue
                    try:
                        if report["removed"]:
                            self.manager.record_removal(op["id"], report)
                        else:
                            self.manager.bind_owned_candidate(op["id"], report)
                            self.manager.observe(op["id"], report)
                    except ValueError as exc:
                        self.manager.uncertain(op["id"], str(exc)[:128])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self._failed(exc)

    def peer_error(self, peer: str, session_id: str, srp: int, errors: list[dict[str, Any]]) -> None:
        try:
            for op in self.manager.list():
                if op["peer"] != peer or op["status"] in {"REJECTED", "ROLLED_BACK"}:
                    continue
                for boundary in (op.get("send"), op.get("completion"), op.get("cleanup")):
                    if boundary and boundary["session_id"] == session_id and boundary["srp_id"] == srp:
                        self.manager.uncertain(op["id"], f"PCC rejected creation or owned cleanup: {errors}"[:128])
                        break
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self._failed(exc)

    def disconnected(self, peer: str) -> None:
        try:
            for op in self.manager.list():
                if op["peer"] == peer and op["status"] in {"PUSHING", "APPLIED_UNVERIFIED"}:
                    self.manager.uncertain(op["id"], "pcep_session_disconnected_reconciliation_required")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self._failed(exc)

    async def remove_owned(self, principal: str, op_id: str) -> dict[str, Any]:
        """Private lab qualification cleanup, never a public dashboard route."""
        async with self._control_lock:
            op = self.manager.get(op_id, principal)
            connection = self._connection(op["peer"])
            assert self.server is not None
            async with connection.send_lock:
                bound = op.get("owned_candidate") or op.get("reported")
                if self._connection(op["peer"]) is not connection or not bound:
                    raise ValueError("Cleanup requires a current bound owned identity")
                identity = bound["identity"]
                candidates = [r for r in connection.state["reports"].values() if
                              r["plsp_id"] == identity["plsp_id"] and r["identifiers"] == identity["identifiers"] and
                              r["symbolic_name_hex"] == identity["symbolic_name_hex"] and r["created"] and
                              r["delegate"] and not r["removed"]]
                if len(candidates) != 1 or connection.srp_sequence >= 0xFFFFFFFE:
                    raise ValueError("Cleanup requires exactly one fresh created and delegated owned LSP")
                srp = connection.srp_sequence + 1
                raw = encode_delete(srp, identity["plsp_id"], op["symbolic_name"])
                self.manager.mark_removing(op_id, connection.state["session_id"], srp,
                                           hashlib.sha256(raw).hexdigest())
                connection.srp_sequence = srp
                try:
                    connection.writer.write(raw)
                    await asyncio.wait_for(connection.writer.drain(), self.server.config.handshake_timeout)
                    connection.last_tx = time.monotonic()
                    self.server._event(op["peer"], "tx", raw, operation_id=op_id, owned_cleanup=True)
                except asyncio.CancelledError:
                    try:
                        self.manager.uncertain(op_id, "owned_cleanup_cancelled_reconciliation_required")
                    except (ValueError, OSError) as exc:
                        self._failed(exc)
                    raise
                except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                    self.manager.uncertain(op_id, str(exc)[:128] or type(exc).__name__)
                    raise
            return self.manager.get(op_id, principal)

    def _current_owned(self, connection: _Connection, op: dict[str, Any]) -> dict[str, Any]:
        bound = op.get("owned_candidate") or op.get("reported")
        if not bound:
            raise ValueError("A durably bound owned identity is required")
        identity = bound["identity"]
        matches = [r for r in connection.state["reports"].values() if
                   r["plsp_id"] == identity["plsp_id"] and r["identifiers"] == identity["identifiers"] and
                   r["symbolic_name_hex"] == identity["symbolic_name_hex"] and r["created"] and
                   r["delegate"] and not r["removed"]]
        if len(matches) != 1:
            raise ValueError("Exactly one unchanged current owned identity is required")
        return matches[0]

    async def reconcile_owned(self, principal: str, op_id: str) -> dict[str, Any]:
        async with self._control_lock:
            op = self.manager.get(op_id, principal)
            connection = self._connection(op["peer"])
            async with connection.send_lock:
                if self._connection(op["peer"]) is not connection:
                    raise ValueError("PCC session changed before ownership reconciliation")
                return self.manager.reconcile_owned(op_id, self._current_owned(connection, op))

    async def complete_owned(self, principal: str, op_id: str) -> dict[str, Any]:
        """One private qualification update; public API never invokes this."""
        async with self._control_lock:
            op = self.manager.get(op_id, principal)
            connection = self._connection(op["peer"])
            assert self.server is not None
            async with connection.send_lock:
                if self._connection(op["peer"]) is not connection or connection.srp_sequence >= 0xFFFFFFFE:
                    raise ValueError("PCC session changed or SRP sequence exhausted")
                report = self._current_owned(connection, op)
                srp = connection.srp_sequence + 1
                raw = encode_owned_update(srp, report, tuple(op["ero"]), op["bandwidth_bps"])
                self.manager.mark_completion(op_id, connection.state["session_id"], srp,
                                             hashlib.sha256(raw).hexdigest())
                connection.srp_sequence = srp
                try:
                    connection.writer.write(raw)
                    await asyncio.wait_for(connection.writer.drain(), self.server.config.handshake_timeout)
                    connection.last_tx = time.monotonic()
                    self.server._event(op["peer"], "tx", raw, operation_id=op_id, owned_completion=True)
                except BaseException:
                    self.manager.uncertain(op_id, "owned_completion_delivery_uncertain")
                    raise
            return self.manager.get(op_id, principal)

    async def _watch(self) -> None:
        assert self.server is not None
        try:
            while True:
                await asyncio.sleep(1)
                for op in self.manager.list():
                    completion = op.get("completion")
                    if completion and not completion.get("acknowledged") and op["status"] != "ROLLBACK_REQUIRED" and (
                            time.time() - datetime.fromisoformat(completion["attempted_at"]).timestamp() >
                            self.server.config.update_ack_timeout):
                        self.manager.uncertain(op["id"], "owned_completion_acknowledgment_timeout")
                    if op["status"] == "PUSHING" and (
                            time.time() - datetime.fromisoformat(
                                op["send"]["attempted_at"]).timestamp() > self.server.config.update_ack_timeout):
                        self.manager.uncertain(op["id"], "acknowledgment_timeout_reconciliation_required")
        except Exception as exc:
            # A dead monitor must close the write gate, not silently abandon
            # timeout handling while the private control socket remains usable.
            self._failed(exc)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or self._control_lock.locked():
                raise ValueError("Private creation console is unavailable")
            raw = await asyncio.wait_for(reader.readline(), 3)
            if len(raw) > 4096 or not raw.endswith(b"\n"):
                raise ValueError("Invalid bounded control request")
            request = json.loads(raw, object_pairs_hook=_json_object,
                                 parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(request, dict):
                raise ValueError("Control request must be an object")
            principal = f"local-uid:{uid}"
            action = request.get("action")
            if action == "create" and set(request) == {"action", "key", "intent"}:
                result: Any = await self.create(principal, request["key"], request["intent"])
            elif action == "get" and set(request) == {"action", "id"}:
                result = self.manager.get(request["id"], principal)
            elif action == "list" and set(request) == {"action"}:
                result = self.manager.list(principal)
            elif action == "remove-owned" and set(request) == {"action", "id"}:
                result = await self.remove_owned(principal, request["id"])
            elif action == "reconcile-owned" and set(request) == {"action", "id"}:
                result = await self.reconcile_owned(principal, request["id"])
            elif action == "complete-owned" and set(request) == {"action", "id"}:
                result = await self.complete_owned(principal, request["id"])
            else:
                raise ValueError("Unknown private control action")
            response = {"ok": True, "result": result}
        except (ValueError, TypeError, KeyError, OSError, RecursionError, struct.error, CspfNoPath, CspfSearchLimit,
                AttributeError, asyncio.TimeoutError) as exc:
            response = {"ok": False, "error": str(exc)[:256]}
        try:
            writer.write(json.dumps(response, allow_nan=False).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 3)
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
