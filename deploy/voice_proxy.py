"""Route-allowlisting proxy in front of the voice profile's Hermes api_server (docs/ARCHITECTURE.md).

The api_server key unlocks every route (/v1/runs, /api/jobs, /api/sessions, room members ...), and /v1/runs
accepts a client-built room execution policy that picks any toolset, terminal included. So Home Assistant never
holds that key: it holds the proxy key (DPAPI hermes_api_key), and the proxy forwards only chat completions (body
reduced to model, messages, stream), the model list and a health check, with the upstream key
(DPAPI hermes_voice_upstream_key).

    python -m deploy.voice_proxy        (run hidden by the Hermes-Voice-Proxy logon task)
"""
from __future__ import annotations

import hmac
import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deploy import secrets, state

PORT = 8642  # what HA calls (host.docker.internal:8642)
UPSTREAM = ("127.0.0.1", 8643)  # the voice profile's api_server, loopback only
UPSTREAM_TIMEOUT = 600  # per socket read: a non-streamed tool run is silent until it ends
MAX_BODY = 1 << 20
MAX_HANDLERS = 16
ROUTES = {("GET", "/health"), ("GET", "/v1/models"), ("POST", "/v1/chat/completions")}
SESSION_HEADERS = ("X-Hermes-Session-Id", "X-Hermes-Session-Key")
RETURN_HEADERS = ("Content-Type", "Content-Length", "X-Hermes-Session-Id", "X-Hermes-Completed", "X-Hermes-Error")


def sanitize_chat(raw: bytes) -> bytes:
    """Keep only model, messages and stream. Hermes reads more (provider, model_options, metadata ...);
    none of it is needed by HA and none of it may reach Hermes. ValueError when malformed."""
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("messages must be a non-empty list")
    out = {"messages": body["messages"]}
    if isinstance(body.get("model"), str):
        out["model"] = body["model"]
    if isinstance(body.get("stream"), bool):
        out["stream"] = body["stream"]
    return json.dumps(out).encode()


class CappedServer(ThreadingHTTPServer):
    """At most MAX_HANDLERS connections in flight (counted from accept, so a flood of silent sockets is covered
    too); the rest get an immediate 503."""
    daemon_threads = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.slots = threading.BoundedSemaphore(MAX_HANDLERS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            return self.shutdown_request(request)
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(client_key: str, upstream_key: str, upstream: tuple[str, int] = UPSTREAM):
    expected = client_key.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-voice-proxy"
        timeout = 30  # socket timeout: a stalled client frees its thread
        sys_version = ""

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def _error(self, status: int, code: str, message: str) -> None:
            data = json.dumps({"error": {"code": code, "message": message}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            if (method, path) not in ROUTES:
                return self._error(404, "not_found", "route not allowed")
            if path == "/health":
                return self._ok()  # liveness of the proxy only; the watchdog probes Hermes on 8643 itself
            auth = self.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), expected)):
                return self._error(401, "unauthorized", "invalid key")
            body = None
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if not 0 < length <= MAX_BODY:
                        return self._error(413 if length else 400, "invalid_request", "body size")
                    body = sanitize_chat(self.rfile.read(length))
                except ValueError:
                    return self._error(400, "invalid_request", "invalid chat request")
            headers = {"Authorization": f"Bearer {upstream_key}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            for name in SESSION_HEADERS:
                value = self.headers.get(name)
                if value and len(value) <= 256:
                    headers[name] = value
            conn = http.client.HTTPConnection(*upstream, timeout=UPSTREAM_TIMEOUT)
            try:
                try:
                    conn.request(method, path, body=body, headers=headers)
                    resp = conn.getresponse()
                except OSError:
                    return self._error(502, "upstream_unavailable", "Hermes voice gateway is not answering")
                # HTTP/1.0 reply: without Content-Length the body (an event stream) ends when we close.
                self.send_response(resp.status)
                for name in RETURN_HEADERS:
                    value = resp.getheader(name)
                    if value:
                        self.send_header(name, value)
                self.end_headers()
                while chunk := resp.read1(65536):
                    self.wfile.write(chunk)
            except OSError:
                pass  # client or upstream went away mid-stream
            finally:
                conn.close()

        def _ok(self) -> None:
            data = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> int:
    host = state.load().get("voice_proxy_host", "127.0.0.1")
    handler = make_handler(secrets.get("hermes_api_key"), secrets.get("hermes_voice_upstream_key"))
    server = CappedServer((host, PORT), handler)
    print(f"voice proxy listening on {host}:{PORT}, upstream {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
