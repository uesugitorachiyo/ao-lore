"""Explicitly started, single-operator loopback viewer. Not a hosted web framework."""
from __future__ import annotations

import copy
import hashlib
import hmac
import re
import secrets
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Callable

from .assets import HTML, JAVASCRIPT, CSS
from .contracts import ViewerError, canonical_json, reject, require, strict_json, validate_key, validate_resolve
from .formats import render_pdf, text_projection
from .store import SourceStore

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
OPEN_PATH = re.compile(r"/api/open/([A-Za-z0-9_-]{43})/(text|original|page/([1-9][0-9]{0,3}))\Z")


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class ViewerState:
    def __init__(self, store: SourceStore, *, renderer: Callable = render_pdf,
                 timer: Callable[[], float] = time.monotonic):
        self.store, self.renderer, self.timer = store, renderer, timer
        self.lock = threading.RLock()
        self.launch_code = secrets.token_urlsafe(32)
        self.launch_deadline = timer() + 120
        self.launch_used = False
        self.failed_logins = 0
        self.sessions: dict[str, float] = {}
        self.handles: dict[str, dict[str, Any]] = {}
        self.audit: deque[dict[str, Any]] = deque(maxlen=256)

    def note(self, code: str, status: int) -> None:
        with self.lock:
            self.audit.append({"event": code, "status": status})

    def _purge(self) -> None:
        now = self.timer()
        self.sessions = {key: until for key, until in self.sessions.items() if until > now}
        self.handles = {key: item for key, item in self.handles.items()
                        if item["until"] > now and item["session"] in self.sessions}

    def login(self, code: Any) -> str:
        self.store.authorize()
        with self.lock:
            if (self.launch_used or self.timer() >= self.launch_deadline or self.failed_logins >= 10
                    or not isinstance(code, str) or not TOKEN.fullmatch(code)
                    or not hmac.compare_digest(code, self.launch_code)):
                self.failed_logins += 1
                reject("session_denied", 401)
            token = secrets.token_urlsafe(32)
            self.sessions[fingerprint(token)] = self.timer() + 1200
            self.launch_used = True
            self.launch_code = ""
            self.note("session_started", 200)
            return token

    def authenticate(self, authorization: str | None) -> str:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            reject("session_required", 401)
        token = authorization[7:]
        if not TOKEN.fullmatch(token):
            reject("session_required", 401)
        key = fingerprint(token)
        with self.lock:
            self._purge()
            if key not in self.sessions:
                reject("session_expired", 401)
        self.store.authorize()
        return key

    def _session_live(self, session: str) -> None:
        with self.lock:
            self._purge()
            if session not in self.sessions:
                reject("session_expired", 401)
        self.store.authorize()

    def cards(self, session: str) -> dict[str, Any]:
        self._session_live(session)
        cards = []
        for record in self.store.list_records():
            evidence = record["evidence"]
            cards.append({key: evidence[key] for key in
                          ("evidence_id", "workspace_id", "generation_digest", "source_id",
                           "authority_role", "freshness_status", "sensitivity")}
                         | {"format": record["format"]})
        return {"primary_workspace_id": self.store.manifest["primary_workspace_id"],
                "provenance_mode": self.store.provenance_mode, "evidence": cards}

    def resolve(self, session: str, key_value: Any) -> dict[str, Any]:
        self._session_live(session)
        record = self.store.get_record(validate_key(key_value))
        data = self.store.source_bytes(record)
        evidence = record["evidence"]
        cited = evidence["source_span"].get("page") if record["format"] == "pdf" else None
        display: dict[str, Any] = {"format": record["format"], "cited_page": cited}
        if record["format"] == "pdf":
            try:
                rendered = self.renderer(data, cited or 1, evidence["render_text"] if cited else "")
                display.update({key: value for key, value in rendered.items() if key != "png"})
                display["render_status"] = "available"
            except ViewerError as error:
                if error.code != "pdf_renderer_unavailable":
                    raise
                display.update(render_status="pdf_renderer_unavailable", page_count=None,
                               highlight={"status": "not_verified", "boxes": []})
        else:
            projection = text_projection(data, record)
            display.update(display_label=projection["display_label"],
                           highlight={"status": projection["segments"]["status"], "boxes": []})
        # Do not release evidence after an approval expires during rendering.
        self._session_live(session)
        with self.lock:
            self._purge()
            if len(self.handles) >= 128:
                reject("open_handle_limit", 429)
            handle = secrets.token_urlsafe(32)
            until = min(self.timer() + 120, self.sessions[session])
            self.handles[fingerprint(handle)] = {
                "session": session, "until": until, "record": record,
            }
        self.note("evidence_resolved", 200)
        readback = {
            "schema_version": ("ao.lore.source-viewer-resolve.v0.2"
                               if self.store.native_binding_revalidated
                               else "ao.lore.source-viewer-resolve.v0.1"),
            "provenance_mode": self.store.provenance_mode,
            "primary_workspace_id": self.store.manifest["primary_workspace_id"],
            "open_id": handle, "expires_in_seconds": max(0, int(until - self.timer())),
            "evidence": copy.deepcopy(evidence), "original_sensitivity": record["original_sensitivity"],
            "original_bytes_verified": True,
            "native_binding_revalidated": self.store.native_binding_revalidated,
            "original_download_allowed": self.store.grant["allow_original_download"],
            "display": display,
        }
        validate_resolve(readback)
        return readback

    def _opened(self, session: str, handle: str) -> tuple[dict[str, Any], bytes]:
        self._session_live(session)
        require(isinstance(handle, str) and bool(TOKEN.fullmatch(handle)))
        with self.lock:
            self._purge()
            opened = self.handles.get(fingerprint(handle))
            if opened is None or opened["session"] != session:
                reject("open_handle_unavailable", 404)
            record = copy.deepcopy(opened["record"])
        return record, self.store.source_bytes(record)

    def page(self, session: str, handle: str, number: int) -> bytes:
        record, data = self._opened(session, handle)
        require(record["format"] == "pdf", "unsupported_format")
        # Geometry is returned in resolve. Other pages do not claim the cited passage.
        result = self.renderer(data, number, "")
        self._session_live(session)
        self.note("source_page_opened", 200)
        return result["png"]

    def text(self, session: str, handle: str) -> dict[str, Any]:
        record, data = self._opened(session, handle)
        projection = text_projection(data, record)
        self._session_live(session)
        self.note("source_text_opened", 200)
        return projection

    def original(self, session: str, handle: str) -> tuple[bytes, str]:
        self._session_live(session)
        if not self.store.grant["allow_original_download"]:
            reject("original_download_denied")
        record, data = self._opened(session, handle)
        self.note("original_downloaded", 200)
        return data, {"pdf": "source.pdf", "docx": "source.docx", "text": "source.txt"}[record["format"]]

    def logout(self, session: str) -> None:
        with self.lock:
            self.sessions.pop(session, None)
            self._purge()
        self.note("session_ended", 200)


class BoundedLoopbackServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, state: ViewerState):
        self.state = state
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(("127.0.0.1", 0), ViewerHandler)
        self.expected_host = "127.0.0.1:" + str(self.server_port)
        self.origin = "http://" + self.expected_host

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def verify_request(self, request, client_address):
        return client_address[0] == "127.0.0.1"

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        self.state.note("connection_rejected", 400)


class ViewerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AO-Lore-Source-Viewer"
    sys_version = ""

    def log_message(self, format, *args):
        # Never persist tokens, source text, client URLs, filesystem paths or headers.
        return

    def send_error(self, code, message=None, explain=None):
        self.respond(code, canonical_json({"error": "request_rejected"}), "application/json")

    def respond(self, status: int, data: bytes, content_type: str,
                extra: dict[str, str] | None = None) -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' blob:; connect-src 'self'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _request_boundary(self) -> None:
        require(len(self.raw_requestline) <= 2048, "request_rejected")
        require(sum(len(k) + len(v) + 4 for k, v in self.headers.items()) <= 16384,
                "request_rejected")
        for name in ("Host", "Origin", "Authorization", "Content-Length", "Content-Type",
                     "Sec-Fetch-Site", "Transfer-Encoding"):
            require(len(self.headers.get_all(name, [])) <= 1, "request_rejected")
        require(self.headers.get("Host") == self.server.expected_host, "origin_denied")
        origin = self.headers.get("Origin")
        require(origin in (None, self.server.origin), "origin_denied")
        if self.command == "POST":
            require(origin == self.server.origin, "origin_denied")
        require(self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none"),
                "origin_denied")
        require(self.headers.get("Transfer-Encoding") is None, "request_rejected")
        # No URL decoding, query strings, fragments, absolute-form URLs or escaped paths.
        require(bool(re.fullmatch(r"/[A-Za-z0-9_./-]*", self.path))
                and ".." not in self.path and "//" not in self.path, "request_rejected")
        if self.command != "POST":
            require(self.headers.get("Content-Length") in (None, "0"), "request_rejected")

    def _body(self) -> Any:
        require(self.headers.get("Content-Type") == "application/json", "request_rejected")
        length = self.headers.get("Content-Length", "")
        require(bool(re.fullmatch(r"[0-9]{1,5}", length)), "request_rejected")
        size = int(length)
        require(0 < size <= 8192, "request_rejected")
        data = self.rfile.read(size)
        require(len(data) == size, "request_rejected")
        return strict_json(data, 8192)

    def _dispatch(self) -> None:
        self._request_boundary()
        state = self.server.state
        if self.command == "GET" and self.path == "/favicon.ico":
            self.respond(204, b"", "image/x-icon")
            return
        if self.command == "GET" and self.path in {"/", "/app.js", "/style.css"}:
            asset, media = {
                "/": (HTML, "text/html; charset=utf-8"),
                "/app.js": (JAVASCRIPT, "text/javascript; charset=utf-8"),
                "/style.css": (CSS, "text/css; charset=utf-8"),
            }[self.path]
            self.respond(200, asset.encode("utf-8"), media)
            return
        if self.command == "POST" and self.path == "/api/session":
            body = self._body()
            require(isinstance(body, dict) and set(body) == {"launch_code"}, "request_rejected")
            token = state.login(body["launch_code"])
            self.respond(200, canonical_json({"session_token": token, "expires_in_seconds": 1200}),
                         "application/json")
            return
        # All remaining routes require a session BEFORE evidence/handle lookup.
        session = state.authenticate(self.headers.get("Authorization"))
        if self.command == "GET" and self.path == "/api/evidence":
            result = state.cards(session)
        elif self.command == "POST" and self.path == "/api/resolve":
            result = state.resolve(session, self._body())
        elif self.command == "POST" and self.path == "/api/logout":
            require(self._body() == {}, "request_rejected")
            state.logout(session)
            result = {"status": "session_ended"}
        elif self.command == "GET" and (match := OPEN_PATH.fullmatch(self.path)):
            handle, action, page = match.groups()
            if action.startswith("page/"):
                self.respond(200, state.page(session, handle, int(page)), "image/png")
                return
            if action == "original":
                data, filename = state.original(session, handle)
                self.respond(200, data, "application/octet-stream",
                             {"Content-Disposition": 'attachment; filename="' + filename + '"'})
                return
            result = state.text(session, handle)
        else:
            reject("route_unavailable", 404)
        self.respond(200, canonical_json(result), "application/json; charset=utf-8")

    def _handle(self) -> None:
        try:
            self._dispatch()
        except ViewerError as error:
            self.server.state.note(error.code, error.status)
            self.respond(error.status, canonical_json({"error": error.code}), "application/json")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.server.state.note("operation_rejected", 400)
            self.respond(400, b'{"error":"operation_rejected"}', "application/json")

    do_GET = _handle
    do_POST = _handle

    def do_HEAD(self):
        self.send_error(405)

    do_OPTIONS = do_HEAD
    do_PUT = do_HEAD
    do_DELETE = do_HEAD
    do_PATCH = do_HEAD
    do_TRACE = do_HEAD
    do_CONNECT = do_HEAD


def make_server(store: SourceStore, *, renderer: Callable = render_pdf) -> BoundedLoopbackServer:
    return BoundedLoopbackServer(ViewerState(store, renderer=renderer))
