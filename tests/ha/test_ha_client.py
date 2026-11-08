"""D4 timeouts and D5 malformed gateway payloads, against a fake HTTP session (needs aiohttp for its types)."""

import asyncio
import json

import pytest

from hvload import load

pytest.importorskip("aiohttp")
client = load("client")


class FakeContent:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line if isinstance(line, bytes) else line.encode()

        return gen()


class FakeResponse:
    def __init__(self, status=200, body=None, lines=None, headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content_type = "text/event-stream" if lines is not None else "application/json"
        self.content = FakeContent(lines or [])
        self.exited = False

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.exited = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.timeouts = []

    def _call(self, url, **kw):
        self.timeouts.append(kw.get("timeout"))
        return self.response

    get = post = _call


def make(response):
    return client.HermesClient(FakeSession(response), "https://h.example/v1", "key")


CHAT = {"model": "m", "user_text": "hi", "system_prompt": None, "session_id": "s", "user_key": None, "request_timeout": 30}


def sse(*frames):
    return [f"data: {json.dumps(f)}\n" if not isinstance(f, str) else f"{f}\n" for f in frames] + ["data: [DONE]\n"]


# --- D5 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [[], "x", None, 3, {"data": "x"}, {"data": {"id": "m"}}, {}])
def test_d5_model_list_malformed(body):
    with pytest.raises(client.HermesError):
        asyncio.run(make(FakeResponse(body=body)).async_list_models())


def test_model_list_ignores_junk_entries():
    body = {"data": [{"id": "a"}, "x", {"id": 5}, {"nope": 1}, None, {"id": "b"}]}
    assert asyncio.run(make(FakeResponse(body=body)).async_list_models()) == ["a", "b"]


@pytest.mark.parametrize(
    "body",
    [
        [],
        "x",
        None,
        {},
        {"choices": {}},
        {"choices": "abc"},
        {"choices": []},
        {"choices": [None]},
        {"choices": ["abc"]},
        {"choices": [{"message": "abc"}]},
        {"choices": [{"message": {"content": ["a", "b"]}}]},
        {"choices": [{"message": {"content": 5}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "  "}}]},
        {"choices": [{"message": {"content": "x"}, "finish_reason": "error"}], "error": {"message": ["x"]}},
        {"choices": [{"message": {"content": "x"}}], "hermes": {"failed": True, "error": {"a": 1}}},
    ],
)
def test_d5_chat_json(body):
    with pytest.raises(client.HermesError):
        asyncio.run(make(FakeResponse(body=body)).async_chat(**CHAT))


@pytest.mark.parametrize("status", [400, 429, 500, 502, 503])
@pytest.mark.parametrize("body", [[], "x", None, {"error": "x"}, {"error": {"message": ["x"]}}, {"error": 5}])
def test_d5_error_statuses_with_odd_bodies(status, body):
    with pytest.raises(client.HermesError):
        asyncio.run(make(FakeResponse(status=status, body=body)).async_chat(**CHAT))


def test_d5_unparseable_json_body_is_a_hermes_error():
    with pytest.raises(client.HermesError):
        asyncio.run(make(FakeResponse(body=ValueError("bad json"))).async_chat(**CHAT))


@pytest.mark.parametrize(
    "frames",
    [
        [{"choices": {}}],
        [{"choices": "abc"}],
        [{"choices": [None]}],
        [{"choices": ["abc"]}],
        [{"choices": [{"delta": "abc"}]}],
        [{"choices": [{"delta": {"content": 5}}]}],
        [{"choices": [{"delta": {"content": ["a"]}}]}],
        [{"choices": [{"delta": {}, "finish_reason": "error"}], "error": ["x"]}],
        ["[1, 2]", "{broken"],
    ],
)
def test_d5_stream_malformed_frames(frames):
    async def consume():
        async for _ in make(FakeResponse(lines=sse(*frames))).async_chat_stream(**CHAT):
            pass

    with pytest.raises(client.HermesError):
        asyncio.run(consume())


def test_stream_still_works():
    frames = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ]

    async def consume():
        return [e.text async for e in make(FakeResponse(lines=sse(*frames))).async_chat_stream(**CHAT)]

    assert asyncio.run(consume()) == ["Hel", "lo"]


# --- D4 ---------------------------------------------------------------------------------------------


def test_d4_connect_silence_and_total_timeouts_are_separate():
    c = make(FakeResponse())
    t = c._timeout(120, streaming=True)
    assert (t.total, t.sock_connect, t.sock_read) == (120, client.CONNECT_TIMEOUT, client.FIRST_BYTE_TIMEOUT)
    t = c._timeout(120, streaming=False)  # a non-streamed run is silent until it ends
    assert (t.total, t.sock_read) == (120, 120)
    t = c._timeout(3, streaming=True)  # never longer than the total
    assert (t.total, t.sock_connect, t.sock_read) == (3, 3, 3)


def test_d4_requests_use_those_timeouts():
    session = FakeSession(FakeResponse(body={"choices": [{"message": {"content": "hi"}}]}))
    c = client.HermesClient(session, "https://h.example", "k", connect_timeout=2, first_byte_timeout=9)
    asyncio.run(c.async_chat(**{**CHAT, "request_timeout": 60}))
    t = session.timeouts[-1]
    assert (t.total, t.sock_connect, t.sock_read) == (60, 2, 60)


def test_d4_closing_the_stream_early_closes_the_request():
    resp = FakeResponse(lines=sse({"choices": [{"delta": {"content": "one"}}]}, {"choices": [{"delta": {"content": "two"}}]}))

    async def go():
        gen = make(resp).async_chat_stream(**CHAT)
        first = await gen.__anext__()
        assert first.text == "one" and not resp.exited
        await gen.aclose()  # what contextlib.aclosing does when HA gives up
        return resp.exited

    assert asyncio.run(go())


def test_gateway_get_helper_that_took_arbitrary_urls_is_gone():
    assert not hasattr(client.HermesClient, "async_get_json")
