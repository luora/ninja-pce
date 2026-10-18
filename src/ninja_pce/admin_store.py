"""Private observation workspaces, immutable revisions and journaled activation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .application import _new_workspace
from .controller import load_controller_config
from .state import SCHEMA_VERSION, digest, write_state

_REVISION = re.compile(r"[a-f0-9]{64}\Z")
_MAX_INPUT = 1048576


class AdminError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def strict_json(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON fields are not accepted")
            result[key] = value
        return result

    def constant(_value: str) -> Any:
        raise ValueError("Nonfinite JSON numbers are not accepted")

    if len(raw) > _MAX_INPUT:
        raise ValueError("Administration input exceeds the size limit")
    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Administration input must be bounded UTF-8 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Administration input must be a JSON object")
    return result


def private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or
            stat.S_IMODE(metadata.st_mode) != 0o700):
        raise ValueError("Administration directories must be owned by this account with mode 0700")
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Administration directory parents must not be symbolic links")


def read_private(path: Path, *, checksummed: bool = True) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
            raise ValueError("Administration files must be private ordinary files owned by this account")
        result = strict_json(stream.read(_MAX_INPUT + 1))
    if checksummed:
        checksum = result.pop("checksum", None)
        if type(result.get("version")) is not int or result["version"] != SCHEMA_VERSION or checksum != digest(result):
            raise ValueError("Administration state checksum or schema is invalid")
    return result


def _optional(path: Path) -> dict[str, Any]:
    try:
        return read_private(path)
    except FileNotFoundError:
        return {}


def _exclusive(path: Path, contents: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock_handle(path: Path) -> Any:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    handle = os.fdopen(descriptor, "r+b")
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
            stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
        handle.close()
        raise ValueError("Administration locks must be private ordinary files owned by this account")
    return handle


def initialize_admin(directory: str | Path) -> tuple[Path, str]:
    from .admin_auth import AuthStore

    root = _new_workspace(directory)
    for relative in ("admin", "revisions", "data", "data/live", "data/state", "data/run"):
        (root / relative).mkdir(mode=0o700)
    write_state(root / "admin/workspace.json", {"kind": "ninja_admin_workspace", "operating_mode": "observe"})
    token = AuthStore(root / "admin").initialize()
    return root, token


class AdminStore:
    """One management owner. State paths and executable selection are server-owned."""

    def __init__(self, directory: str | Path, *, grpcurl: str = "grpcurl", gobgpd: str | None = None):
        from .admin_runtime import ObserverRuntime

        self.root = Path(os.path.abspath(Path(directory).expanduser()))
        self.admin = self.root / "admin"
        for relative in (".", "admin", "revisions", "data", "data/live", "data/state", "data/run"):
            private_directory(self.root / relative)
        marker = read_private(self.admin / "workspace.json")
        if marker.get("kind") != "ninja_admin_workspace" or marker.get("operating_mode") != "observe":
            raise ValueError("Choose a separately initialized observation administration workspace")
        self.grpcurl = str(Path(grpcurl).absolute()) if "/" in grpcurl else grpcurl
        self.gobgpd = str(Path(gobgpd).absolute()) if gobgpd is not None else None
        self._lock_file = _lock_handle(self.admin / "serve.lock")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise AdminError(409, "ADMIN_ALREADY_RUNNING", "This workspace already has a management owner.") from None
        self._mutex = threading.RLock()
        self._closed = False
        self._previews: dict[str, dict[str, Any]] = {}
        try:
            self.runtime = ObserverRuntime(self.root, self.grpcurl, self.gobgpd)
            transaction = self.transaction()
            if transaction.get("status") in {"APPLYING", "ACTIVE"}:
                transaction.update(status="UNCERTAIN", message="Management lost its owned process handles; reconcile from the local CLI.")
                write_state(self.admin / "transaction.json", transaction)
                self._audit("system", "RECOVERY_REQUIRED", transaction.get("target_revision"))
        except BaseException:
            self._lock_file.close()
            raise

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._mutex:
            if self._closed:
                raise AdminError(503, "ADMIN_STOPPING", "Management is shutting down.")
            for relative in (".", "admin", "revisions", "data", "data/live", "data/state", "data/run"):
                private_directory(self.root / relative)
            yield

    def close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closed = True
            try:
                transaction = self.transaction()
                runtime = self.runtime.status()
                owns_live_child = any(isinstance(item, dict) and item.get("state") == "running" for item in runtime.values())
                if (self.runtime.running or owns_live_child or self.runtime.uncertain or
                        transaction.get("status") == "ACTIVE") and transaction.get("status") != "UNCERTAIN":
                    revision = self.current_revision()
                    transaction = {"kind": "admin_transaction", "status": "APPLYING", "actor": "system",
                                   "previous_revision": revision, "target_revision": revision, "action": "shutdown"}
                    write_state(self.admin / "transaction.json", transaction)
                    try:
                        self.runtime.stop()
                        transaction.update(status="SAVED")
                    except Exception:
                        transaction.update(status="UNCERTAIN", message="Owned process shutdown was not confirmed; reconcile locally.")
                    write_state(self.admin / "transaction.json", transaction)
                else:
                    self.runtime.stop()
            finally:
                self._lock_file.close()

    def current_revision(self) -> str | None:
        active = _optional(self.admin / "active.json")
        if not active:
            return None
        revision = active.get("revision")
        if active.get("kind") != "admin_active_revision" or not (revision is None or self._valid_revision(revision)):
            raise ValueError("Invalid active administration revision")
        return revision

    @staticmethod
    def _valid_revision(value: Any) -> bool:
        return isinstance(value, str) and _REVISION.fullmatch(value) is not None

    def transaction(self) -> dict[str, Any]:
        result = _optional(self.admin / "transaction.json")
        if result and result.get("kind") != "admin_transaction":
            raise ValueError("Invalid administration transaction")
        return result

    def _writable(self) -> None:
        self.runtime.status()
        if self.runtime.uncertain and self.transaction().get("status") != "UNCERTAIN":
            revision = self.current_revision()
            write_state(self.admin / "transaction.json", {"kind": "admin_transaction", "status": "UNCERTAIN",
                        "previous_revision": revision, "target_revision": revision,
                        "message": "Owned process cleanup could not be confirmed; reconcile locally."})
        if self.transaction().get("status") in {"APPLYING", "UNCERTAIN"}:
            raise AdminError(409, "RECONCILIATION_REQUIRED", "An interrupted operation requires local reconciliation.")

    def _compare(self, expected: Any) -> str | None:
        current = self.current_revision()
        if expected != current:
            raise AdminError(409, "REVISION_CONFLICT", "The active revision changed. Reload and review a new preview.")
        return current

    def _set_active(self, revision: str | None) -> None:
        write_state(self.admin / "active.json", {"kind": "admin_active_revision", "revision": revision})

    def _audit(self, actor: str, action: str, revision: Any, **details: Any) -> None:
        document = _optional(self.admin / "audit.json")
        if document and (document.get("kind") != "admin_audit" or not isinstance(document.get("events"), list)):
            raise ValueError("Invalid administration audit state")
        events = document.get("events", [])[-255:]
        events.append({"at": time.time(), "actor": actor, "action": action, "revision": revision, **details})
        write_state(self.admin / "audit.json", {"kind": "admin_audit", "events": events})

    def audit(self) -> dict[str, Any]:
        with self.locked():
            result = _optional(self.admin / "audit.json")
            return {"events": result.get("events", [])}

    def _revision_files(self, revision: str) -> dict[str, bytes]:
        from .admin_model import render_setup, validate_setup

        if not self._valid_revision(revision):
            raise ValueError("Invalid administration revision identifier")
        directory = self.root / "revisions" / revision
        private_directory(directory)
        manifest = read_private(directory / "manifest.json")
        hashes = manifest.get("files")
        if manifest.get("kind") != "admin_revision" or manifest.get("revision") != revision or not isinstance(hashes, dict):
            raise ValueError("Invalid administration revision inventory")
        setup = read_private(directory / "setup.json", checksummed=False)
        expected_revision = digest({"setup": validate_setup(setup), "grpcurl": self.grpcurl, "gobgpd": self.gobgpd})
        expected = render_setup(setup, self.root, directory, grpcurl=self.grpcurl)
        if revision != expected_revision or set(hashes) != set(expected) or set(p.name for p in directory.iterdir()) != {*expected, "manifest.json"}:
            raise ValueError("Administration revision no longer matches the trusted setup and executable selection")
        for name, contents in expected.items():
            path = directory / name
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                        stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1):
                    raise ValueError("Revision payload must be a private ordinary file")
                actual = stream.read(_MAX_INPUT + 1)
            if actual != contents or hashes[name] != hashlib.sha256(actual).hexdigest():
                raise ValueError("Administration revision payload was modified")
        load_controller_config(directory / "controller.json")
        return expected

    def configuration(self) -> dict[str, Any]:
        from .admin_model import default_setup

        with self.locked():
            revision = self.current_revision()
            source_management = "managed" if self.gobgpd is not None else "external"
            if revision is None:
                return {"revision": None, "setup": default_setup(), "source_management": source_management}
            payload = self._revision_files(revision)
            return {"revision": revision, "setup": strict_json(payload["setup.json"]), "source_management": source_management}

    def preview(self, setup: dict[str, Any], expected: Any, actor: str) -> dict[str, Any]:
        from .admin_model import render_setup, validate_setup

        with self.locked():
            self._writable()
            previous = self._compare(expected)
            canonical = validate_setup(setup)
            revision = digest({"setup": canonical, "grpcurl": self.grpcurl, "gobgpd": self.gobgpd})
            directory = self.root / "revisions" / revision
            payload = render_setup(canonical, self.root, directory, grpcurl=self.grpcurl)
            if not directory.exists():
                if len(list((self.root / "revisions").iterdir())) >= 256:
                    raise AdminError(409, "REVISION_LIMIT", "Archive this workspace before creating further revisions.")
                directory.mkdir(mode=0o700)
                try:
                    for name, contents in payload.items():
                        _exclusive(directory / name, contents)
                    load_controller_config(directory / "controller.json")
                    write_state(directory / "manifest.json", {"kind": "admin_revision", "revision": revision,
                                "files": {name: hashlib.sha256(contents).hexdigest() for name, contents in payload.items()}})
                    _sync_directory(directory)
                    _sync_directory(directory.parent)
                except BaseException:
                    # Only this invocation's new, unpublished candidate is removed.
                    shutil.rmtree(directory)
                    raise
            self._revision_files(revision)
            previous_payload = self._revision_files(previous) if previous is not None else {}
            changes = [name for name in sorted(set(payload) | set(previous_payload))
                       if payload.get(name) != previous_payload.get(name)]
            now = time.monotonic()
            self._previews = {key: value for key, value in self._previews.items() if value["expires"] > now}
            if len(self._previews) >= 16:
                del self._previews[next(iter(self._previews))]
            identifier = secrets.token_urlsafe(32)
            self._previews[identifier] = {"revision": revision, "expected": previous, "actor": actor, "expires": now + 300}
            self._audit(actor, "PREVIEW", revision, previous_revision=previous)
            return {"preview_id": identifier, "expected_revision": previous, "revision": revision,
                    "files": {name: contents.decode() for name, contents in payload.items()},
                    "changes": changes, "write_authority": False}

    def apply(self, preview_id: Any, expected: Any, actor: str, *, activate: bool = False) -> dict[str, Any]:
        with self.locked():
            self._writable()
            previous = self._compare(expected)
            preview = self._previews.get(preview_id) if isinstance(preview_id, str) else None
            if (not preview or preview["actor"] != actor or preview["expected"] != previous or
                    preview["expires"] <= time.monotonic()):
                raise AdminError(409, "PREVIEW_REQUIRED", "Review a fresh preview under this administrator before applying.")
            if type(activate) is not bool:
                raise ValueError("activate must be a boolean")
            target = preview["revision"]
            self._revision_files(target)
            was_running = self.runtime.running
            if was_running and not activate:
                raise AdminError(409, "ACTIVATION_REQUIRED", "The observer is running. Apply with activation to keep runtime and configuration aligned.")
            transaction = {"kind": "admin_transaction", "id": secrets.token_hex(16), "actor": actor,
                           "previous_revision": previous, "target_revision": target,
                           "previous_running": was_running, "activate": activate, "status": "APPLYING"}
            self._audit(actor, "APPLYING", target, previous_revision=previous)
            write_state(self.admin / "transaction.json", transaction)
            del self._previews[preview_id]
            try:
                self.runtime.stop()
                self._set_active(target)
                if activate:
                    self.runtime.start(self.root / "revisions" / target)
                transaction.update(status="ACTIVE" if activate else "SAVED", message=(
                    "Observer processes started; network readiness is evaluated separately." if activate else
                    "Configuration saved. Start the observer when ready."))
            except Exception as error:
                try:
                    self.runtime.stop()
                    if self.runtime.uncertain:
                        raise ValueError("Owned runtime shutdown is uncertain")
                    self._set_active(previous)
                    if was_running and previous is not None:
                        self._revision_files(previous)
                        self.runtime.start(self.root / "revisions" / previous)
                    transaction.update(status="ROLLED_BACK", message="Activation failed; the previous configuration was restored.",
                                       error=str(error)[:1024])
                except Exception:
                    transaction.update(status="UNCERTAIN", message="Activation or rollback could not be confirmed; reconcile locally.")
            write_state(self.admin / "transaction.json", transaction)
            self._audit(actor, transaction["status"], self.current_revision(), previous_revision=previous)
            return {"revision": self.current_revision(), "status": transaction["status"],
                    "message": transaction["message"], "runtime": self.runtime.status(), "transaction": transaction}

    def control_runtime(self, action: str, expected: Any, actor: str) -> dict[str, Any]:
        with self.locked():
            self._writable()
            revision = self._compare(expected)
            if action not in {"start", "stop"}:
                raise ValueError("Runtime action must be start or stop")
            if action == "start" and revision is None:
                raise AdminError(409, "CONFIGURATION_REQUIRED", "Save a reviewed configuration before starting the observer.")
            if action == "start":
                runtime = self.runtime.status()
                if (self.runtime.running or any(isinstance(item, dict) and item.get("state") == "running"
                                               for item in runtime.values())):
                    raise AdminError(409, "OBSERVER_ALREADY_OWNED", "This management process already owns observer processes. Stop them before starting again.")
            transaction: dict[str, Any] = {"kind": "admin_transaction", "status": "APPLYING", "actor": actor,
                           "previous_revision": revision, "target_revision": revision, "action": action}
            self._audit(actor, action.upper(), revision)
            write_state(self.admin / "transaction.json", transaction)
            try:
                if action == "stop":
                    self.runtime.stop()
                else:
                    assert revision is not None
                    self._revision_files(revision)
                    self.runtime.start(self.root / "revisions" / revision)
                transaction.update(status="ACTIVE" if action == "start" else "SAVED")
            except Exception as error:
                transaction.update(status="UNCERTAIN" if self.runtime.uncertain else "FAILED", error=str(error)[:1024])
            write_state(self.admin / "transaction.json", transaction)
            self._audit(actor, transaction["status"], revision)
            return self.status()

    def snapshot(self) -> dict[str, Any]:
        from .dashboard import DashboardConfig, DashboardStore

        if not self._mutex.acquire(blocking=False):
            snapshot = DashboardStore(DashboardConfig(operating_mode="observe",
                state_dir=self.root / "data/state", topology_path=self.root / "data/live/topology.json",
                bgpls_status_path=self.root / "data/live/bgpls-status.json",
                bgpls_raw_path=self.root / "data/live/bgpls-raw.json")).snapshot()
            snapshot["readiness"] = self._busy_readiness()
            return snapshot
        try:
            return self._snapshot_locked()
        finally:
            self._mutex.release()

    @staticmethod
    def _busy_readiness() -> dict[str, Any]:
        return {"state": "CONFIGURING", "ready": False, "scope": "runtime", "operating_mode": "observe",
                "message": "An administration operation is in progress; current readiness is unconfirmed.",
                "startup_findings": [], "control_authority_verified": False, "forwarding_verified": False}

    def _snapshot_locked(self) -> dict[str, Any]:
        from .dashboard import DashboardConfig, DashboardStore
        from .live import _ospf_multi_area_policy
        from .readiness import evaluate_readiness

        with self.locked():
            revision = self.current_revision()
            if revision is None:
                config = DashboardConfig(operating_mode="observe", state_dir=self.root / "data/state",
                                         topology_path=self.root / "data/live/topology.json",
                                         bgpls_status_path=self.root / "data/live/bgpls-status.json",
                                         bgpls_raw_path=self.root / "data/live/bgpls-raw.json")
            else:
                self._revision_files(revision)
                data = load_controller_config(self.root / "revisions" / revision / "controller.json").document
                config = DashboardConfig(operating_mode="observe", state_dir=Path(data["state_dir"]),
                                         topology_path=Path(data["files"]["topology"]),
                                         bgpls_status_path=Path(data["files"]["bgpls_status"]),
                                         bgpls_raw_path=Path(data["files"]["bgpls_raw"]),
                                         required_bgpls_peers=tuple(data["bgpls"]["required_peers"]),
                                         max_age_seconds=data["bgpls"]["max_age_seconds"],
                                         ospf_multi_area=_ospf_multi_area_policy(data["bgpls"].get("ospf_multi_area_policy")))
            snapshot = DashboardStore(config).snapshot()
            running = self.runtime.status()
            records = [{"component": "bgpls", "state": "running" if running.get("controller", {}).get("state") == "running" else "stopped"}]
            snapshot["readiness"] = evaluate_readiness(snapshot, "observe", records)
            if self.transaction().get("status") == "UNCERTAIN":
                snapshot["readiness"] = {**self._busy_readiness(), "state": "CONFIGURATION_UNCERTAIN",
                    "message": "Owned process cleanup is unconfirmed. Stop management and reconcile through the local admin CLI."}
            snapshot["admin_revision"] = revision
            return snapshot

    def status(self) -> dict[str, Any]:
        if not self._mutex.acquire(blocking=False):
            return {"revision": self.current_revision(), "transaction": self.transaction(),
                    "runtime": {"state": "starting", "message": "An administration operation is in progress."},
                    "readiness": self._busy_readiness()}
        try:
            with self.locked():
                transaction = self.transaction()
                runtime = self.runtime.status()
                if transaction.get("status") == "UNCERTAIN":
                    runtime.update(state="uncertain", running=False,
                                   message="Owned process cleanup is unconfirmed. Stop management and run admin reconcile locally.")
                return {"revision": self.current_revision(), "transaction": transaction,
                        "runtime": runtime, "readiness": self.snapshot()["readiness"]}
        finally:
            self._mutex.release()

    def reconcile(self) -> dict[str, Any]:
        """Explicit local recovery, after ownership checks; no foreign process changes."""
        from .controller import _process_identity
        from .startup import require_available_listener

        with self.locked():
            transaction = self.transaction()
            if transaction.get("status") not in {"APPLYING", "UNCERTAIN"}:
                raise ValueError("No interrupted operation needs reconciliation")
            handles = []
            try:
                for component in ("bgpls", "pcep", "dashboard"):
                    path = self.root / "data/run" / f".ninja-pce-controller-{component}.lock"
                    handle = _lock_handle(path)
                    handles.append(handle)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    record = _optional(path.with_suffix(".json"))
                    child = record.get("child", {})
                    if (isinstance(child, dict) and type(child.get("pid")) is int and
                            isinstance(child.get("start_ticks"), str) and
                            _process_identity(child["pid"]) == child["start_ticks"]):
                        raise AdminError(409, "LIVE_COMPONENT_REMAINS", "A retained component child is still alive; review its managed shutdown before reconciling.")
                writer = _lock_handle(self.root / "data/run/.ninja-pce-bgpls-writer.lock")
                handles.append(writer)
                try:
                    fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise AdminError(409, "LIVE_COLLECTOR_REMAINS", "A collector still owns observation outputs; review its managed shutdown before reconciling.") from error
                if self.gobgpd is not None:
                    require_available_listener("127.0.0.1", 50051, component="gobgpd")
                    for revision in {transaction.get("previous_revision"), transaction.get("target_revision")} - {None}:
                        if not isinstance(revision, str):
                            raise ValueError("Interrupted operation contains an invalid revision")
                        self._revision_files(revision)
                        setup = read_private(self.root / "revisions" / revision / "setup.json", checksummed=False)
                        settings = setup["controller"]
                        if settings["bgp_port"] > 0:
                            require_available_listener(settings["source_address"], settings["bgp_port"], component="gobgpd")
                previous = transaction.get("previous_revision")
                if previous is not None:
                    self._revision_files(previous)
                self._set_active(previous)
                transaction.update(status="RECOVERED", message="Previous configuration restored; observer remains stopped.")
                write_state(self.admin / "transaction.json", transaction)
                self._audit("local-cli", "RECOVERED", previous)
            finally:
                for handle in handles:
                    handle.close()
            return {"revision": previous, "status": "RECOVERED"}
