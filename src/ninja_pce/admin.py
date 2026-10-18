"""Opt-in, loopback-only authenticated observation administration."""
from __future__ import annotations

import argparse
import json
import signal
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .admin_auth import AuthError, AuthStore
from .admin_store import AdminError, AdminStore, initialize_admin, strict_json
from .dashboard import _DashboardServer

DEFAULT_ADMIN = ".ninja-pce/admin-workspace"
_COOKIE = "ninja_admin"


def create_admin_server(store: AdminStore, *, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Administration requires IPv4 loopback and a port between 0 and 65535")
    auth = AuthStore(store.admin)
    public_assets = {"/setup": ("setup.html", "text/html; charset=utf-8"),
                     "/setup.js": ("setup.js", "text/javascript; charset=utf-8"),
                     "/setup.css": ("setup.css", "text/css; charset=utf-8")}
    private_assets = {"/dashboard": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/styles.css": ("styles.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "NinjaPCE-Admin"
        sys_version = ""

        def log_message(self, *_args: Any) -> None:
            pass

        def respond(self, status: int, value: Any, *, mime: str = "application/json", cookie: str | None = None,
                    location: str | None = None) -> None:
            body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            for name, content in (("Content-Type", mime), ("Content-Length", str(len(body))),
                                  ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                                  ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                                  ("Connection", "close")):
                self.send_header(name, content)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def check_request(self, *, mutation: bool = False) -> str:
            for name in ("Host", "Origin", "Cookie", "Content-Length", "Content-Type", "X-CSRF-Token"):
                if len(self.headers.get_all(name, [])) > 1:
                    raise AdminError(400, "AMBIGUOUS_REQUEST", "Duplicate security or framing headers are not accepted.")
            if len(self.headers.as_bytes()) > 16384 or self.headers.get("Transfer-Encoding") is not None:
                raise AdminError(400, "INVALID_FRAMING", "Oversized headers or transfer encoding are not accepted.")
            authority = f"127.0.0.1:{server.server_port}"
            if self.headers.get("Host") != authority:
                raise AdminError(403, "HOST_REJECTED", "Use the configured loopback administration URL.")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise AdminError(403, "ORIGIN_REJECTED", "Cross-site administration requests are not accepted.")
            if mutation and self.headers.get("Origin") != f"http://{authority}":
                raise AdminError(403, "ORIGIN_REJECTED", "An exact same-origin administration request is required.")
            parsed = urlsplit(self.path)
            if not self.path.startswith("/") or parsed.netloc or parsed.scheme or parsed.query or parsed.fragment:
                raise AdminError(400, "INVALID_TARGET", "Administration routes do not accept alternate origins or query parameters.")
            return parsed.path

        def token(self) -> str:
            value = self.headers.get("Cookie", "")
            if len(value) > 4096 or sum(part.partition("=")[0].strip() == _COOKIE for part in value.split(";")) > 1:
                raise AdminError(400, "INVALID_COOKIE", "Ambiguous session cookies are not accepted.")
            cookie: SimpleCookie = SimpleCookie()
            try:
                cookie.load(value)
            except CookieError:
                return ""
            item = cookie.get(_COOKIE)
            return item.value if item is not None else ""

        def session(self) -> dict[str, Any]:
            result = auth.authenticate(self.token())
            if result is None:
                raise AdminError(401, "AUTHENTICATION_REQUIRED", "Sign in to administer this workspace.")
            return result

        def body(self, *, maximum: int = 1048576) -> dict[str, Any]:
            length = self.headers.get("Content-Length", "")
            if not length.isascii() or not length.isdigit() or len(length) > 7 or int(length) > maximum:
                raise AdminError(413, "BODY_REJECTED", "A bounded JSON body and Content-Length are required.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise AdminError(415, "JSON_REQUIRED", "Use application/json for administration requests.")
            count = int(length)
            raw = self.rfile.read(count)
            if len(raw) != count:
                raise ValueError("The request body was incomplete")
            return strict_json(raw)

        @staticmethod
        def fields(document: dict[str, Any], required: set[str], optional: set[str] | None = None) -> None:
            if not required <= document.keys() or document.keys() - required - (optional or set()):
                raise ValueError("The request has missing or unsupported fields")

        def get(self) -> None:
            path = self.check_request()
            if path == "/healthz":
                self.respond(200, {"status": "ok", "management": True})
                return
            if path == "/":
                self.respond(302, b"", location="/setup")
                return
            if path in public_assets:
                name, mime = public_assets[path]
                self.respond(200, files("ninja_pce").joinpath("web").joinpath(name).read_bytes(), mime=mime)
                return
            if path == "/api/admin/session":
                session = auth.authenticate(self.token())
                value: dict[str, Any] = {"bootstrap_required": auth.bootstrap_required, "authenticated": session is not None}
                if session is not None:
                    value.update(username=session["username"], csrf_token=session["csrf_token"])
                self.respond(200, value)
                return
            self.session()
            if path in private_assets:
                name, mime = private_assets[path]
                contents = files("ninja_pce").joinpath("web").joinpath(name).read_bytes()
                if name == "index.html":
                    contents = contents.replace(b"<body>", b'<body><a class="admin-setup-link" href="/setup">Controller setup</a>')
                self.respond(200, contents, mime=mime)
            elif path == "/api/admin/config":
                self.respond(200, store.configuration())
            elif path == "/api/admin/status":
                self.respond(200, store.status())
            elif path == "/api/admin/audit":
                self.respond(200, store.audit())
            elif path == "/api/controller":
                self.respond(200, store.snapshot())
            elif path == "/readyz":
                readiness = store.snapshot()["readiness"]
                self.respond(200 if readiness["ready"] else 503, readiness)
            elif path == "/api/tunnel-options":
                self.respond(200, {"enabled": False, "headends": [], "destinations": [], "max_bandwidth_bps": 0,
                                   "reason": "Observation setup does not enable tunnel provisioning.", "policy": {"read_only": True}})
            else:
                self.respond(404, {"error": "NOT_FOUND", "message": "Unknown administration route."})

        def post(self) -> None:
            path = self.check_request(mutation=True)
            if path in {"/api/admin/bootstrap", "/api/admin/login"}:
                document = self.body(maximum=4096)
                self.fields(document, {"username", "password", "token"} if path.endswith("bootstrap") else {"username", "password"})
                session = (auth.claim(document["token"], document["username"], document["password"])
                           if path.endswith("bootstrap") else auth.login(document["username"], document["password"]))
                self.respond(200, {"authenticated": True, "username": session["username"], "csrf_token": session["csrf_token"]},
                             cookie=f"{_COOKIE}={session['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800")
                return
            session = self.session()
            if not auth.csrf_valid(session, self.headers.get("X-CSRF-Token", "")):
                raise AdminError(403, "CSRF_REJECTED", "A current session CSRF token is required.")
            document = self.body()
            actor = session["username"]
            if path == "/api/admin/logout":
                self.fields(document, set())
                auth.logout(self.token())
                self.respond(200, {"authenticated": False}, cookie=f"{_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            elif path == "/api/admin/preview":
                self.fields(document, {"expected_revision", "setup"})
                self.respond(200, store.preview(document["setup"], document["expected_revision"], actor))
            elif path == "/api/admin/apply":
                self.fields(document, {"expected_revision", "preview_id"}, {"activate"})
                self.respond(200, store.apply(document["preview_id"], document["expected_revision"], actor,
                                              activate=document.get("activate", False)))
            elif path == "/api/admin/runtime":
                self.fields(document, {"expected_revision", "action"})
                self.respond(200, store.control_runtime(document["action"], document["expected_revision"], actor))
            else:
                self.respond(404, {"error": "NOT_FOUND", "message": "Unknown administration route."})

        def dispatch(self, *, mutation: bool = False) -> None:
            try:
                self.post() if mutation else self.get()
            except (AdminError, AuthError) as error:
                self.respond(error.status, {"error": error.code, "message": str(error)[:1024]})
            except (ValueError, TypeError, RecursionError):
                self.respond(400, {"error": "INVALID_INPUT", "message": "Invalid input or local administration state. Review the typed settings and local file permissions."})
            except OSError:
                self.respond(503, {"error": "LOCAL_STATE_UNAVAILABLE", "message": "Local administration state is unavailable; inspect permissions and service logs."})

        def do_GET(self) -> None:
            self.dispatch()

        do_HEAD = do_GET

        def do_POST(self) -> None:
            self.dispatch(mutation=True)

        def reject(self) -> None:
            self.respond(405, {"error": "METHOD_REJECTED", "message": "Use the documented GET and POST routes."})

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_CONNECT = reject

    server = _DashboardServer((host, port), Handler)
    return server


def add_admin_commands(subparsers: Any) -> None:
    admin = subparsers.add_parser("admin", help="Authenticated first-launch observation setup")
    commands = admin.add_subparsers(dest="admin_action", required=True)
    initialize = commands.add_parser("init", help="Create a new private admin workspace and one-time enrollment token")
    initialize.add_argument("--directory", default=DEFAULT_ADMIN)
    token = commands.add_parser("bootstrap-token", help="Rotate an unused bootstrap token through local access")
    token.add_argument("--directory", default=DEFAULT_ADMIN)
    for action in ("serve", "reconcile"):
        command = commands.add_parser(action, help="Run loopback setup" if action == "serve" else "Reconcile an interrupted activation locally")
        command.add_argument("--directory", default=DEFAULT_ADMIN)
        command.add_argument("--grpcurl", default="grpcurl", help="Trusted installed grpcurl executable")
        command.add_argument("--gobgpd", help="Trusted installed GoBGP binary; omit to use an external loopback speaker")
        if action == "serve":
            command.add_argument("--port", default=8080, type=int)
            command.add_argument("--managed-startup", action="store_true",
                                 help="Return exit 78 for permanent managed administration startup faults")


def admin_command(args: argparse.Namespace) -> int:
    if args.admin_action == "init":
        directory, token = initialize_admin(args.directory)
        print(f"Private observation administration workspace: {directory}")
        print(f"One-time enrollment token (expires in 30 minutes): {token}")
        print(f"Start setup: ninja-pce admin serve --directory {directory}")
        return 0
    if args.admin_action == "bootstrap-token":
        directory = Path(args.directory).expanduser().absolute()
        from .admin_store import private_directory, read_private

        private_directory(directory)
        if read_private(directory / "admin/workspace.json").get("kind") != "ninja_admin_workspace":
            raise ValueError("Choose a valid administration workspace")
        print(AuthStore(directory / "admin").rotate_bootstrap())
        return 0
    store = AdminStore(args.directory, grpcurl=args.grpcurl, gobgpd=args.gobgpd)
    try:
        if args.admin_action == "reconcile":
            print(json.dumps(store.reconcile(), sort_keys=True))
            return 0
        server = create_admin_server(store, port=args.port)
        stop = threading.Event()
        previous: dict[int, Any] = {}
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda _signum, _frame: stop.set())
            worker.start()
            print(f"Ninja-PCE authenticated setup: http://127.0.0.1:{server.server_port}/setup", flush=True)
            while not stop.wait(.5):
                if not worker.is_alive():
                    raise OSError("The administration HTTP loop exited unexpectedly")
        finally:
            if worker.is_alive():
                server.shutdown()
            server.server_close()
            if worker.ident is not None:
                worker.join(timeout=2)
            for previous_signum, handler in previous.items():
                signal.signal(previous_signum, handler)
        return 0
    finally:
        store.close()
