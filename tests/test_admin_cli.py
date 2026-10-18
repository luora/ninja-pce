"""First launch through the installed command surface, using isolated local state."""
from __future__ import annotations

import http.client
import json
import re
import selectors
import socket
import subprocess
import sys

from ninja_pce.admin_model import default_setup
from ninja_pce.admin_store import initialize_admin, read_private
from ninja_pce.cli import main


def serve(root, *extra):
    process = subprocess.Popen([sys.executable, "-I", "-m", "ninja_pce.cli", "admin", "serve",
                                "--directory", str(root), "--port", "0", *extra],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert selector.select(timeout=8), "Management did not publish a local startup URL"
        line = process.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:(\d+)/setup", line)
        assert match, line
        return process, int(match[1])
    except BaseException:
        stop(process)
        raise
    finally:
        selector.close()


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def request(port, route, document=None, *, cookie=None, csrf=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if document is not None:
        headers.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"})
    try:
        connection.request("GET" if document is None else "POST", route,
                           body=json.dumps(document) if document is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


def test_init_and_unused_token_rotation_never_import_an_existing_workspace(tmp_path, capsys):
    root = tmp_path / "fresh"
    assert main(["admin", "init", "--directory", str(root)]) == 0
    output = capsys.readouterr().out
    original = re.search(r"minutes\): ([A-Za-z0-9_-]{43})", output)
    assert original
    assert main(["admin", "bootstrap-token", "--directory", str(root)]) == 0
    rotated = capsys.readouterr().out.strip()
    assert len(rotated) == 43 and rotated != original[1]
    marker = root / "data/state/ownership.json"
    marker.write_bytes(b"retained independent ownership")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert main(["admin", "init", "--directory", str(root)]) == 1
    assert {path: path.read_bytes() for path in before} == before


def test_managed_admin_validation_and_port_conflict_use_permanent_exit(tmp_path, capsys):
    assert main(["admin", "serve", "--directory", str(tmp_path / "absent"), "--managed-startup"]) == 78
    assert json.loads(capsys.readouterr().err)["code"] == "STARTUP_LOCAL_RESOURCE_ERROR"
    root, _ = initialize_admin(tmp_path / "fresh")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert main(["admin", "serve", "--directory", str(root), "--managed-startup", "--port",
                     str(occupied.getsockname()[1])]) == 78
        assert json.loads(capsys.readouterr().err)["code"] == "STARTUP_LOCAL_RESOURCE_ERROR"
        assert occupied.fileno() >= 0


def test_real_authenticated_cli_setup_activation_shutdown_and_session_restart(tmp_path):
    root, token = initialize_admin(tmp_path / "fresh")
    client = tmp_path / "grpcurl"
    client.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('Synthetic unavailable source\\n')\nsys.exit(1)\n")
    client.chmod(0o700)
    journal = root / "data/state/retained-ownership.json"
    journal.write_bytes(b"original LSP owner, held reservations and recovery history")
    original = journal.read_bytes()
    process, port = serve(root, "--grpcurl", str(client))
    try:
        assert request(port, "/healthz")[0] == 200
        assert request(port, "/api/admin/config")[0] == 401
        code, session, headers = request(port, "/api/admin/bootstrap",
                                        {"token": token, "username": "operator", "password": "a sufficiently long private password"})
        assert code == 200
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        csrf = session["csrf_token"]
        document = default_setup()
        code, preview, _ = request(port, "/api/admin/preview", {"expected_revision": None, "setup": document}, cookie=cookie, csrf=csrf)
        assert code == 200
        code, applied, _ = request(port, "/api/admin/apply", {"expected_revision": None, "preview_id": preview["preview_id"], "activate": True}, cookie=cookie, csrf=csrf)
        assert code == 200 and applied["status"] == "ACTIVE", applied
        assert applied["runtime"]["controller"]["state"] == "running"
        assert request(port, "/healthz")[0] == 200
        code, status, _ = request(port, "/api/admin/status", cookie=cookie)
        assert code == 200 and not status["readiness"]["ready"]
        assert status["readiness"]["operating_mode"] == "observe"
        configuration = json.loads((root / "revisions" / preview["revision"] / "controller.json").read_text())
        assert configuration["operating_mode"] == "observe" and "pcep" not in configuration
        assert not (root / "data/live/pcep.json").exists()
        assert journal.read_bytes() == original
        process.terminate()
        assert process.wait(timeout=10) == 0
        assert read_private(root / "admin/transaction.json")["status"] == "SAVED"
        assert journal.read_bytes() == original
    finally:
        stop(process)
    restarted, new_port = serve(root, "--grpcurl", str(client))
    try:
        assert request(new_port, "/api/admin/config", cookie=cookie)[0] == 401
        assert request(new_port, "/api/admin/session", cookie=cookie)[1] == {"bootstrap_required": False, "authenticated": False}
        code, session, headers = request(new_port, "/api/admin/login", {"username": "operator", "password": "a sufficiently long private password"})
        assert code == 200
        new_cookie = headers["Set-Cookie"].split(";", 1)[0]
        status = request(new_port, "/api/admin/status", cookie=new_cookie)[1]
        assert status["revision"] == preview["revision"]
        assert status["runtime"]["state"] == "stopped"
        assert journal.read_bytes() == original
    finally:
        stop(restarted)
