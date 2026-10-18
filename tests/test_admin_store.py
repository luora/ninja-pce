"""Reviewed revisions, ownership and recovery preserve independent state."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import sys
import types

import pytest

from ninja_pce import admin_store
from ninja_pce.admin_auth import AuthStore
from ninja_pce.admin_model import default_setup
from ninja_pce.admin_store import AdminError, AdminStore, initialize_admin, read_private
from ninja_pce.state import write_state


class ObserverDouble:
    """An owned runtime whose startup can fail independently of management."""

    def __init__(self, root, grpcurl, gobgpd):
        self.running = False
        self.uncertain = False
        self.fail_revisions = set()
        self.started = []
        self.stops = 0

    def start(self, directory):
        self.started.append(directory.name)
        if directory.name in self.fail_revisions:
            raise RuntimeError("Owned protocol startup was refused")
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def status(self):
        return {"controller": {"state": "running" if self.running else "stopped"},
                "uncertain": self.uncertain}


@pytest.fixture
def observer_double(monkeypatch):
    try:
        import ninja_pce.admin_runtime as runtime
    except ImportError:
        runtime = types.ModuleType("ninja_pce.admin_runtime")
        monkeypatch.setitem(sys.modules, "ninja_pce.admin_runtime", runtime)
    monkeypatch.setattr(runtime, "ObserverRuntime", ObserverDouble, raising=False)


@pytest.fixture
def workspace(tmp_path):
    root, token = initialize_admin(tmp_path / "observation")
    assert AuthStore(root / "admin").bootstrap_required
    assert len(token) == 43
    return root


@pytest.fixture
def store(workspace, observer_double):
    manager = AdminStore(workspace)
    try:
        yield manager
    finally:
        manager.close()


def failure(code, action, *args, **kwargs):
    with pytest.raises(AdminError) as caught:
        action(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def draft(name):
    setup = default_setup()
    setup["controller"]["name"] = name
    return setup


def saved(store, name="first", actor="operator"):
    preview = store.preview(draft(name), store.current_revision(), actor)
    result = store.apply(preview["preview_id"], preview["expected_revision"], actor)
    assert result["status"] == "SAVED"
    return result["revision"]


def protected_files(root):
    """These bytes represent state already owned by other controller features."""
    contents = {
        "data/state/reservations.json": b"independently held RSVP reservations\n",
        "data/state/provisioning.json": b"existing provisioning operation and original authority\n",
        "data/state/recovery.json": b"DEV recovery attempts and retained ownership\n",
        "data/live/pcep.json": b"retained capture from another observer\n",
    }
    for relative, payload in contents.items():
        path = root / relative
        path.write_bytes(payload)
        path.chmod(0o600)
    return contents


def assert_preserved(root, contents):
    for relative, expected in contents.items():
        assert (root / relative).read_bytes() == expected


def test_workspace_requires_new_isolated_directory(tmp_path, observer_double):
    original = tmp_path / "legacy"
    original.mkdir()
    sentinel = original / "controller.json"
    sentinel.write_text("Existing production configuration")
    with pytest.raises(ValueError, match="already exists"):
        initialize_admin(original)
    assert sentinel.read_text() == "Existing production configuration"


def test_status_and_configuration_work_before_network_setup_without_creating_state(store, workspace):
    before = {str(path.relative_to(workspace)): path.read_bytes()
              for path in workspace.rglob("*") if path.is_file()}
    configuration = store.configuration()
    assert configuration["revision"] is None
    assert configuration["setup"]["operating_mode"] == "observe"
    assert configuration["source_management"] == "external"
    status = store.status()
    assert status["revision"] is None and status["transaction"] == {}
    assert status["runtime"]["controller"]["state"] == "stopped"
    assert status["readiness"]["ready"] is False
    snapshot = store.snapshot()
    assert snapshot["operating_mode"] == "observe" and snapshot["admin_revision"] is None
    assert any(source["id"] == "pcep" and source["state"] == "disabled" for source in snapshot["sources"])
    after = {str(path.relative_to(workspace)): path.read_bytes()
             for path in workspace.rglob("*") if path.is_file()}
    assert after == before


def test_foreign_management_owner_is_refused_without_disturbing_original(store, workspace):
    error = failure("ADMIN_ALREADY_RUNNING", AdminStore, workspace)
    assert error.status == 409
    assert store.configuration()["revision"] is None
    assert not store.runtime.running and store.runtime.stops == 0


def test_runtime_initialization_failure_releases_management_ownership(workspace, observer_double, monkeypatch):
    import ninja_pce.admin_runtime as runtime

    def broken_runtime(*args):
        raise RuntimeError("Local runtime initialization failed")

    monkeypatch.setattr(runtime, "ObserverRuntime", broken_runtime)
    with pytest.raises(RuntimeError, match="initialization failed"):
        AdminStore(workspace)
    monkeypatch.setattr(runtime, "ObserverRuntime", ObserverDouble)
    recovered = AdminStore(workspace)
    try:
        assert recovered.configuration()["revision"] is None
    finally:
        recovered.close()


def test_closed_manager_cannot_mutate_workspace_after_releasing_ownership(store, workspace):
    revision = saved(store)
    store.close()
    replacement = AdminStore(workspace)
    try:
        failure("ADMIN_STOPPING", store.preview, draft("unsafe-old-manager"), revision, "operator")
        assert replacement.current_revision() == revision
        assert replacement.configuration()["setup"]["controller"]["name"] == "first"
        store.close()
    finally:
        replacement.close()


@pytest.mark.parametrize("replacement", ["public", "fifo", "hardlink", "symlink"])
def test_management_owner_lock_must_be_a_private_ordinary_file(workspace, tmp_path, observer_double, replacement):
    path = workspace / "admin/serve.lock"
    target = tmp_path / "foreign-lock"
    target.write_bytes(b"foreign state")
    target.chmod(0o600)
    if replacement == "public":
        path.write_bytes(b"")
        path.chmod(0o644)
    elif replacement == "fifo":
        os.mkfifo(path, 0o600)
    elif replacement == "hardlink":
        os.link(target, path)
    else:
        path.symlink_to(target)
    try:
        manager = AdminStore(workspace)
    except (ValueError, OSError):
        pass
    else:
        manager.close()
        pytest.fail("An unsafe ownership lock must not be accepted")
    assert target.read_bytes() == b"foreign state"


def test_preview_validates_and_generates_observation_files_without_activation(store, workspace):
    state = protected_files(workspace)
    setup = draft("reviewed-controller")
    original = copy.deepcopy(setup)
    preview = store.preview(setup, None, "operator")
    assert setup == original
    assert preview["write_authority"] is False
    assert preview["expected_revision"] is None
    assert store.current_revision() is None and not store.runtime.running
    assert set(preview["files"]) == {"setup.json", "controller.json", "gobgpd.toml", "node-map.json"}
    controller = json.loads(preview["files"]["controller.json"])
    assert controller["operating_mode"] == "observe" and controller["dashboard"]["enabled"] is False
    assert "pcep" not in controller
    assert controller["state_dir"] == str(workspace / "data/state")
    assert 'default-export-policy = "reject-route"' in preview["files"]["gobgpd.toml"]
    assert_preserved(workspace, state)


def test_preview_and_apply_require_current_expected_revision(store):
    first = saved(store)
    failure("REVISION_CONFLICT", store.preview, draft("stale"), None, "operator")
    pending = store.preview(draft("pending"), first, "operator")
    saved(store, "newer")
    failure("REVISION_CONFLICT", store.apply, pending["preview_id"], first, "operator")
    assert store.configuration()["setup"]["controller"]["name"] == "newer"


def test_duplicate_start_retains_active_ownership_recovery_record(store, workspace):
    revision = saved(store)
    store.control_runtime("start", revision, "operator")
    before = (workspace / "admin/transaction.json").read_bytes()
    failure("OBSERVER_ALREADY_OWNED", store.control_runtime, "start", revision, "operator")
    assert store.runtime.running
    assert (workspace / "admin/transaction.json").read_bytes() == before
    assert store.transaction()["status"] == "ACTIVE"


def test_server_reports_native_configuration_ownership_before_setup(workspace, observer_double):
    manager = AdminStore(workspace, gobgpd="/reviewed/test-only/gobgpd")
    try:
        assert manager.configuration()["source_management"] == "managed"
        assert manager.configuration()["revision"] is None
        assert not manager.runtime.started
    finally:
        manager.close()


def test_preview_is_actor_bound_expiring_and_one_use(store, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(admin_store.time, "monotonic", lambda: now[0])
    preview = store.preview(draft("first"), None, "operator")
    failure("PREVIEW_REQUIRED", store.apply, preview["preview_id"], None, "other")
    now[0] = 400
    failure("PREVIEW_REQUIRED", store.apply, preview["preview_id"], None, "operator")
    renewed = store.preview(draft("first"), None, "operator")
    result = store.apply(renewed["preview_id"], None, "operator")
    failure("PREVIEW_REQUIRED", store.apply, renewed["preview_id"], result["revision"], "operator")


@pytest.mark.parametrize("preview_id", [None, [], {}, 1, "unreviewed-token"])
def test_missing_or_untrusted_preview_cannot_publish_configuration(store, preview_id):
    failure("PREVIEW_REQUIRED", store.apply, preview_id, None, "operator")
    assert store.current_revision() is None and store.transaction() == {}


def test_saving_configuration_does_not_start_protocols_or_touch_protected_state(store, workspace):
    state = protected_files(workspace)
    revision = saved(store)
    assert store.current_revision() == revision
    assert store.transaction()["status"] == "SAVED"
    assert store.runtime.started == [] and not store.runtime.running
    assert_preserved(workspace, state)


def test_running_observer_requires_explicit_activation_for_new_revision(store):
    first = saved(store)
    store.control_runtime("start", first, "operator")
    preview = store.preview(draft("new"), first, "operator")
    before_stops = store.runtime.stops
    failure("ACTIVATION_REQUIRED", store.apply, preview["preview_id"], first, "operator")
    assert store.current_revision() == first and store.runtime.running
    assert store.runtime.stops == before_stops
    result = store.apply(preview["preview_id"], first, "operator", activate=True)
    assert result["status"] == "ACTIVE" and result["revision"] == preview["revision"]
    assert store.runtime.running


def test_activation_failure_restores_previous_running_revision_and_preserves_journals(store, workspace):
    state = protected_files(workspace)
    previous = saved(store)
    store.control_runtime("start", previous, "operator")
    preview = store.preview(draft("fails-startup"), previous, "operator")
    store.runtime.fail_revisions.add(preview["revision"])
    result = store.apply(preview["preview_id"], previous, "operator", activate=True)
    assert result["status"] == "ROLLED_BACK" and result["revision"] == previous
    assert store.runtime.running and store.runtime.started[-1] == previous
    assert store.configuration()["setup"]["controller"]["name"] == "first"
    assert_preserved(workspace, state)
    events = store.audit()["events"]
    assert events[-1]["action"] == "ROLLED_BACK" and events[-1]["actor"] == "operator"


def test_first_activation_failure_rolls_back_to_unconfigured_management(store, workspace):
    state = protected_files(workspace)
    preview = store.preview(draft("first-fails"), None, "operator")
    store.runtime.fail_revisions.add(preview["revision"])
    result = store.apply(preview["preview_id"], None, "operator", activate=True)
    assert result["status"] == "ROLLED_BACK" and result["revision"] is None
    assert store.configuration()["revision"] is None and not store.runtime.running
    assert store.status()["readiness"]["ready"] is False
    assert_preserved(workspace, state)


def test_failed_rollback_becomes_uncertain_and_blocks_further_mutations(store, workspace):
    state = protected_files(workspace)
    previous = saved(store)
    store.control_runtime("start", previous, "operator")
    preview = store.preview(draft("failed-target"), previous, "operator")
    store.runtime.fail_revisions.update({previous, preview["revision"]})
    result = store.apply(preview["preview_id"], previous, "operator", activate=True)
    assert result["status"] == "UNCERTAIN"
    failure("RECONCILIATION_REQUIRED", store.preview, draft("new"), store.current_revision(), "operator")
    failure("RECONCILIATION_REQUIRED", store.control_runtime, "start", store.current_revision(), "operator")
    assert_preserved(workspace, state)


def test_restart_recovers_applying_as_uncertain_until_local_reconciliation(workspace, observer_double):
    state = protected_files(workspace)
    first = AdminStore(workspace)
    previous = saved(first)
    preview = first.preview(draft("interrupted"), previous, "operator")
    first.close()
    write_state(workspace / "admin/active.json", {"kind": "admin_active_revision", "revision": preview["revision"]})
    write_state(workspace / "admin/transaction.json", {
        "kind": "admin_transaction", "status": "APPLYING", "previous_revision": previous,
        "target_revision": preview["revision"], "actor": "operator",
    })
    restarted = AdminStore(workspace)
    try:
        assert restarted.transaction()["status"] == "UNCERTAIN"
        assert restarted.current_revision() == preview["revision"]
        assert restarted.runtime.started == []
        failure("RECONCILIATION_REQUIRED", restarted.preview, draft("new"), preview["revision"], "operator")
        result = restarted.reconcile()
        assert result == {"revision": previous, "status": "RECOVERED"}
        assert restarted.current_revision() == previous and not restarted.runtime.running
        assert restarted.transaction()["status"] == "RECOVERED"
        assert restarted.audit()["events"][-1]["actor"] == "local-cli"
        assert_preserved(workspace, state)
    finally:
        restarted.close()


def test_restart_after_active_runtime_loses_handles_and_requires_reconciliation(workspace, observer_double):
    original = AdminStore(workspace)
    revision = saved(original)
    original.close()
    write_state(workspace / "admin/transaction.json", {"kind": "admin_transaction", "status": "ACTIVE",
                "previous_revision": revision, "target_revision": revision, "actor": "operator"})
    restarted = AdminStore(workspace)
    try:
        assert restarted.transaction()["status"] == "UNCERTAIN"
        assert restarted.current_revision() == revision and restarted.runtime.started == []
        status = restarted.status()
        assert status["runtime"]["state"] == "uncertain"
        assert "reconcile" in status["runtime"]["message"]
        assert status["readiness"]["state"] == "CONFIGURATION_UNCERTAIN" and not status["readiness"]["ready"]
        failure("RECONCILIATION_REQUIRED", restarted.preview, draft("new"), revision, "operator")
        assert restarted.reconcile() == {"revision": revision, "status": "RECOVERED"}
        assert not restarted.runtime.running
    finally:
        restarted.close()


def test_retained_live_child_blocks_reconciliation_after_supervisor_lock_is_free(store, workspace):
    from ninja_pce.controller import _process_identity

    revision = saved(store)
    write_state(workspace / "admin/transaction.json", {"kind": "admin_transaction", "status": "UNCERTAIN",
                "previous_revision": revision, "target_revision": revision, "actor": "operator"})
    write_state(workspace / "data/run/.ninja-pce-controller-bgpls.json", {
        "kind": "controller_component", "component": "bgpls", "state": "running",
        "child": {"pid": os.getpid(), "start_ticks": _process_identity(os.getpid())},
    })
    assert failure("LIVE_COMPONENT_REMAINS", store.reconcile).status == 409
    assert store.transaction()["status"] == "UNCERTAIN"
    assert store.current_revision() == revision


@pytest.mark.parametrize("group_running", [True, False])
def test_failed_shutdown_durably_blocks_reuse_even_when_group_health_is_failed(store, workspace, monkeypatch,
                                                                           group_running):
    revision = saved(store)
    store.runtime.running = group_running
    monkeypatch.setattr(store.runtime, "status", lambda: {"controller": {"state": "failed"},
                                                         "gobgpd": {"state": "running"}})

    def uncertain_stop():
        store.runtime.running = False
        store.runtime.uncertain = True
        raise ValueError("Owned descendant cleanup could not be established")

    monkeypatch.setattr(store.runtime, "stop", uncertain_stop)
    store.close()
    transaction = read_private(workspace / "admin/transaction.json")
    assert transaction["status"] == "UNCERTAIN" and transaction["action"] == "shutdown"
    replacement = AdminStore(workspace)
    try:
        failure("RECONCILIATION_REQUIRED", replacement.preview, draft("new"), revision, "operator")
    finally:
        replacement.close()


def test_observed_runtime_uncertainty_is_persisted_before_refusing_writes(store, workspace):
    revision = saved(store)
    store.runtime.uncertain = True
    failure("RECONCILIATION_REQUIRED", store.preview, draft("new"), revision, "operator")
    transaction = read_private(workspace / "admin/transaction.json")
    assert transaction["status"] == "UNCERTAIN" and transaction["previous_revision"] == revision


def test_local_reconciliation_refuses_foreign_component_lock(store, workspace):
    previous = saved(store)
    write_state(workspace / "admin/transaction.json", {"kind": "admin_transaction", "status": "UNCERTAIN",
                "previous_revision": previous, "target_revision": previous, "actor": "operator"})
    descriptor = os.open(workspace / "data/run/.ninja-pce-controller-bgpls.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as foreign:
        fcntl.flock(foreign.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = (workspace / "admin/active.json").read_bytes()
        with pytest.raises((AdminError, OSError)):
            store.reconcile()
        assert (workspace / "admin/active.json").read_bytes() == before
        assert store.transaction()["status"] == "UNCERTAIN"
        assert foreign.fileno() >= 0


@pytest.mark.parametrize("replacement", ["public", "fifo", "hardlink", "symlink"])
def test_local_reconciliation_refuses_unsafe_component_lock(store, workspace, tmp_path, replacement):
    previous = saved(store)
    write_state(workspace / "admin/transaction.json", {"kind": "admin_transaction", "status": "UNCERTAIN",
                "previous_revision": previous, "target_revision": previous, "actor": "operator"})
    path = workspace / "data/run/.ninja-pce-controller-bgpls.lock"
    target = tmp_path / "foreign-lock"
    target.write_bytes(b"foreign state")
    target.chmod(0o600)
    if replacement == "public":
        path.write_bytes(b"")
        path.chmod(0o644)
    elif replacement == "fifo":
        os.mkfifo(path, 0o600)
    elif replacement == "hardlink":
        os.link(target, path)
    else:
        path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        store.reconcile()
    assert target.read_bytes() == b"foreign state"
    assert store.transaction()["status"] == "UNCERTAIN"


def test_payload_tampering_after_preview_cannot_be_applied(store, workspace):
    previous = saved(store)
    preview = store.preview(draft("new"), previous, "operator")
    path = workspace / "revisions" / preview["revision"] / "gobgpd.toml"
    path.write_bytes(path.read_bytes() + b"\n# unreviewed mutation\n")
    before = (workspace / "admin/active.json").read_bytes()
    with pytest.raises(ValueError, match="modified"):
        store.apply(preview["preview_id"], previous, "operator")
    assert (workspace / "admin/active.json").read_bytes() == before
    assert store.transaction()["status"] == "SAVED"


def test_manifest_cannot_inject_arbitrary_filename(store, workspace, tmp_path):
    preview = store.preview(draft("new"), None, "operator")
    manifest_path = workspace / "revisions" / preview["revision"] / "manifest.json"
    manifest = read_private(manifest_path)
    target = tmp_path / "foreign-authority.json"
    target.write_bytes(b"foreign state must not be touched")
    manifest["files"][str(target)] = "0" * 64
    write_state(manifest_path, manifest)
    with pytest.raises(ValueError, match="trusted setup"):
        store.apply(preview["preview_id"], None, "operator")
    assert target.read_bytes() == b"foreign state must not be touched"
    assert store.current_revision() is None


@pytest.mark.parametrize("injected", ["state_dir", "runtime_dir", "files", "pcep", "command", "domain_policy"])
def test_setup_cannot_select_arbitrary_paths_or_authority(store, injected):
    setup = default_setup()
    setup[injected] = "/etc/ninja-pce/existing-control.json"
    with pytest.raises(ValueError, match="unsupported"):
        store.preview(setup, None, "operator")
    assert store.current_revision() is None


def test_symlinked_active_manifest_cannot_be_read_or_overwritten(store, workspace, tmp_path):
    target = tmp_path / "foreign.json"
    write_state(target, {"kind": "admin_active_revision", "revision": None})
    before = target.read_bytes()
    (workspace / "admin/active.json").symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        store.preview(default_setup(), None, "operator")
    assert target.read_bytes() == before
    assert (workspace / "admin/active.json").is_symlink()


def test_symlinked_revision_payload_is_refused(store, workspace, tmp_path):
    preview = store.preview(draft("new"), None, "operator")
    path = workspace / "revisions" / preview["revision"] / "controller.json"
    target = tmp_path / "foreign-controller.json"
    target.write_bytes(path.read_bytes())
    target.chmod(0o600)
    original = target.read_bytes()
    path.unlink()
    path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        store.apply(preview["preview_id"], None, "operator")
    assert target.read_bytes() == original and store.current_revision() is None


def test_trusted_executable_selection_is_bound_into_revision_identity(workspace, observer_double):
    first = AdminStore(workspace)
    preview = first.preview(default_setup(), None, "operator")
    revision = first.apply(preview["preview_id"], None, "operator")["revision"]
    first.close()
    changed = AdminStore(workspace, grpcurl="different-reviewed-grpcurl")
    try:
        with pytest.raises(ValueError, match="trusted setup"):
            changed.configuration()
        assert changed.current_revision() == revision
    finally:
        changed.close()


def test_preview_cannot_survive_management_restart(workspace, observer_double):
    original = AdminStore(workspace)
    preview = original.preview(default_setup(), None, "operator")
    original.close()
    restarted = AdminStore(workspace)
    try:
        failure("PREVIEW_REQUIRED", restarted.apply, preview["preview_id"], None, "operator")
        assert restarted.current_revision() is None
    finally:
        restarted.close()


def test_start_requires_reviewed_configuration_and_stop_never_grants_authority(store):
    failure("CONFIGURATION_REQUIRED", store.control_runtime, "start", None, "operator")
    status = store.control_runtime("stop", None, "operator")
    assert status["revision"] is None and not store.runtime.running
    assert status["transaction"]["status"] == "SAVED"


def test_private_revision_inventory_and_content_permissions(store, workspace):
    revision = saved(store)
    directory = workspace / "revisions" / revision
    assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_nlink == 1
