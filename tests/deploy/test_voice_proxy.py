import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import socket

from deploy import voice_proxy
from deploy.voice_proxy import make_handler, sanitize_chat

CLIENT, UPSTREAM = "client-key-0123456789", "upstream-key-0123456789"


class FakeHermes(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Hermes-Session-Id", "sid-1")
        self.send_header("X-Secret-Internal", "nope")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for part in (body[:5], body[5:]):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
        self.wfile.write(b"0\r\n\r\n")

    def do_GET(self):
        FakeHermes.seen.append(("GET", self.path, dict(self.headers), None))
        self._reply(b'{"data":[{"id":"voice"}]}', "application/json")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHermes.seen.append(("POST", self.path, dict(self.headers), body))
        if body.get("stream"):
            self._reply(b"data: {\"x\":1}\n\ndata: [DONE]\n\n", "text/event-stream")
        else:
            self._reply(b'{"choices":[]}', "application/json")


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


@pytest.fixture
def proxy():
    FakeHermes.seen = []
    hermes = _serve(FakeHermes)
    srv = _serve(make_handler(CLIENT, UPSTREAM, upstream=("127.0.0.1", hermes.server_port)))
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    hermes.shutdown()


def call(base, method, path, key=CLIENT, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {**({"Authorization": f"Bearer {key}"} if key else {}), **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/runs"), ("POST", "/api/jobs"), ("GET", "/api/jobs"), ("GET", "/api/sessions"),
    ("POST", "/v1/responses"), ("POST", "/api/sessions/x/chat"), ("GET", "/v1/toolsets"),
    ("POST", "/v1/room-members/invitations"), ("POST", "/p/default/v1/chat/completions"),
    ("POST", "/v1/chat/completions/../runs"), ("GET", "/health/detailed"), ("PATCH", "/v1/chat/completions"),
])
def test_only_allowlisted_routes_reach_hermes(proxy, method, path):
    status, _, _ = call(proxy, method, path, body={"input": "x"} if method != "GET" else None)
    assert status in (404, 501)
    assert FakeHermes.seen == []


def test_wrong_or_missing_key_is_rejected(proxy):
    assert call(proxy, "GET", "/v1/models", key="wrong")[0] == 401
    assert call(proxy, "GET", "/v1/models", key=UPSTREAM)[0] == 401
    assert call(proxy, "GET", "/v1/models", key=None)[0] == 401
    assert FakeHermes.seen == []


def test_health_needs_no_key_and_never_calls_hermes(proxy):
    assert call(proxy, "GET", "/health", key=None)[0] == 200
    assert FakeHermes.seen == []


def test_chat_is_reduced_and_reauthenticated(proxy):
    body = {"model": "voice", "messages": [{"role": "user", "content": "hi"}], "stream": False,
            "hosted_room_dispatch": {"x": 1}, "_room_execution_policy": {"enabled_toolsets": ["terminal"]},
            "provider": "evil", "model_options": {"reasoning": "max"}}
    status, headers, data = call(proxy, "POST", "/v1/chat/completions", body=body,
                                 headers={"X-Hermes-Session-Id": "alexa-household-kitchen-1",
                                          "X-Hermes-Session-Key": "household", "X-Other": "drop"})
    assert status == 200 and json.loads(data) == {"choices": []}
    assert headers.get("X-Hermes-Session-Id") == "sid-1" and "X-Secret-Internal" not in headers
    method, path, sent_headers, sent = FakeHermes.seen[0]
    assert (method, path) == ("POST", "/v1/chat/completions")
    assert sent == {"messages": body["messages"], "model": "voice", "stream": False}
    assert sent_headers["Authorization"] == f"Bearer {UPSTREAM}"
    assert sent_headers["X-Hermes-Session-Key"] == "household" and "X-Other" not in sent_headers


def test_stream_passes_through(proxy):
    status, headers, data = call(proxy, "POST", "/v1/chat/completions",
                                 body={"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert status == 200 and headers["Content-Type"] == "text/event-stream"
    assert data == b"data: {\"x\":1}\n\ndata: [DONE]\n\n"


def test_models_forwarded(proxy):
    assert call(proxy, "GET", "/v1/models")[0] == 200
    assert FakeHermes.seen[0][:2] == ("GET", "/v1/models")


def test_bad_bodies_rejected_before_hermes(proxy):
    assert call(proxy, "POST", "/v1/chat/completions", body={"messages": []})[0] == 400
    assert call(proxy, "POST", "/v1/chat/completions", body=["x"])[0] == 400
    assert FakeHermes.seen == []


def test_hermes_down_is_502():
    srv = _serve(make_handler(CLIENT, UPSTREAM, upstream=("127.0.0.1", 9)))  # nothing listens on port 9
    try:
        status, _, data = call(f"http://127.0.0.1:{srv.server_port}", "GET", "/v1/models")
        assert status == 502 and json.loads(data)["error"]["code"] == "upstream_unavailable"
    finally:
        srv.shutdown()


def test_sanitize_chat_rejects_non_json():
    with pytest.raises(ValueError):
        sanitize_chat(b"{not json")


def test_handler_has_socket_timeout():
    assert make_handler(CLIENT, UPSTREAM).timeout == 30


def test_flood_of_silent_connections_gets_503_when_full(monkeypatch):
    monkeypatch.setattr(voice_proxy, "MAX_HANDLERS", 2)
    srv = voice_proxy.CappedServer(("127.0.0.1", 0), make_handler(CLIENT, UPSTREAM))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    held = [socket.create_connection(("127.0.0.1", srv.server_port)) for _ in range(2)]  # send nothing: handlers block
    try:
        import time
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", srv.server_port))
        extra.settimeout(5)
        assert extra.recv(64).startswith(b"HTTP/1.0 503")
        extra.close()
    finally:
        for s in held:
            s.close()
        srv.shutdown()


def test_successful_requests_are_not_logged(capsys):
    """Health checks every 2 minutes must not grow the log forever; failures still log."""
    from http import HTTPStatus
    from deploy import voice_proxy
    handler_cls = voice_proxy.make_handler("k", "u", ("127.0.0.1", 1))
    h = handler_cls.__new__(handler_cls)
    h.requestline, h.client_address, h.request_version, h.command = "GET /health HTTP/1.1", ("127.0.0.1", 1), "HTTP/1.1", "GET"
    h.log_request(200)
    h.log_request(HTTPStatus.OK)
    assert capsys.readouterr().err == ""
    h.log_request(502)
    assert "502" in capsys.readouterr().err
