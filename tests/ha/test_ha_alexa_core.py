"""Alexa bridge logic (pure, no Home Assistant)."""

import asyncio
import hashlib

import pytest

from hvload import aio, load

core = load("alexa_core")

GOOD = {
    "text": "what is the weather",
    "alexa_device_id": "amzn1.ask.device.ABC",
    "alexa_user_id": "amzn1.ask.account.XYZ",
    "alexa_session_id": "amzn1.echo-api.session.1",
}


def bad(**changes):
    with pytest.raises(core.RequestError):
        core.validate_request({**GOOD, **changes})


# --- request validation ---------------------------------------------------------------------------


def test_valid_request_gets_defaults():
    req = core.validate_request({**GOOD, "text": "  hi  "})
    assert (req.text, req.kind, req.locale, req.budget_ms, req.has_screen) == ("hi", "utterance", "en-US", 5500, False)
    assert req.alexa_person_id is None


def test_every_field_is_checked():
    bad(unknown=1)
    bad(text="")
    bad(text="x" * 1001)
    bad(text=5)
    bad(text="a\x00b")
    bad(kind="shout")
    bad(kind=["utterance"])
    bad(alexa_device_id="")
    bad(alexa_device_id="has space")
    bad(alexa_device_id="x" * 513)
    bad(alexa_user_id=None)
    bad(alexa_session_id="a/b")
    bad(alexa_person_id="bad id")
    bad(alexa_person_id=7)
    bad(locale="english")
    bad(locale="en_US")
    bad(budget_ms=999)
    bad(budget_ms=7001)
    bad(budget_ms=True)
    bad(budget_ms="5000")
    bad(budget_ms=5000.0)
    bad(has_screen="yes")
    for body in (None, [], "x", 3):
        with pytest.raises(core.RequestError):
            core.validate_request(body)
    with pytest.raises(core.RequestError):
        core.validate_request({k: v for k, v in GOOD.items() if k != "alexa_device_id"})


def test_text_may_be_empty_only_for_resume_launch_end():
    for kind in ("resume", "launch", "end"):
        assert core.validate_request({**GOOD, "text": "", "kind": kind}).text == ""
    bad(text="   ", kind="utterance")
    no_text = {k: v for k, v in GOOD.items() if k != "text"}
    assert core.validate_request({**no_text, "kind": "resume"}).text == ""


def test_error_messages_do_not_echo_long_values():
    with pytest.raises(core.RequestError) as e:
        core.validate_request({**GOOD, "x" * 5000: 1})
    assert len(str(e.value)) < 100


# --- keys -----------------------------------------------------------------------------------------


def test_keys_follow_the_contract():
    dev = "amzn1.ask.device.ABC"
    h = hashlib.sha256(dev.encode()).hexdigest()[:24]
    assert core.device_identifier(dev) == ("hermes_voice", f"alexa:{h}")
    p = hashlib.sha256(b"person1").hexdigest()[:16]
    assert core.speaker_key("person1") == f"person:{p}"
    assert core.speaker_key(None) == "household"
    assert core.hermes_session_id("household", "Living Room", dev, 1000) == "alexa-household-living-room-1000"
    assert core.hermes_session_id("household", None, dev, 1000) == f"alexa-household-{h[:12]}-1000"
    sid = core.hermes_session_id(core.speaker_key("p"), "Kid's Room!", dev, 5)
    assert all(c.isalnum() or c in "-:" for c in sid)  # Hermes accepts [A-Za-z0-9_.:-]


def test_window_continues_until_30_idle_minutes():
    now = [1000.0]
    w = core.WindowTracker(clock=lambda: now[0])
    assert w.start("k") == 1000
    now[0] += 29 * 60
    assert w.start("k") == 1000  # continues
    now[0] += 29 * 60  # 29 min since the last turn, 58 since the start
    assert w.start("k") == 1000
    now[0] += 31 * 60
    assert w.start("k") == int(now[0])  # idle too long: new window
    assert w.start("other") == int(now[0])
    assert len(w) == 2


def test_window_tracker_is_capped():
    now = [0.0]
    w = core.WindowTracker(clock=lambda: now[0], max_keys=3)
    for i in range(10):
        now[0] += 1
        w.start(f"k{i}")
    assert len(w) == 3


# --- room commands --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "room"),
    [
        ("this is the kitchen", "Kitchen"),
        ("This is the Kitchen.", "Kitchen"),
        ("this device is in the living room", "Living Room"),
        ("This device is in the living room, please", "Living Room"),
        ("this echo is in my bedroom", "Bedroom"),
        ("you are in the office", "Office"),
        ("you're in the office", "Office"),
        ("you’re in the office", "Office"),
        ("this is the master bedroom", "Master Bedroom"),
        ("alexa this is the garage", "Garage"),
        ("this speaker is in the guest suite", "Guest Suite"),  # explicit form: any name
        ("set the room to the nursery", "Nursery"),
    ],
)
def test_room_commands(text, room):
    assert core.parse_room_command(text) == room


@pytest.mark.parametrize(
    "text",
    [
        "this is the best pizza ever made",
        "this is a test",
        "this is the weather",
        "you are in trouble",
        "this speaker is awesome",
        "what is the capital of france",
        "this is the room",
        "turn on the kitchen light",
        "this device is in the very very very very very big room",
        "",
    ],
)
def test_not_room_commands(text):
    assert core.parse_room_command(text) is None


def test_known_areas_make_unusual_names_work_everyday():
    assert core.parse_room_command("this is the sauna", ["Sauna"]) == "Sauna"
    assert core.parse_room_command("this is the sauna") is None
    assert core.parse_room_command("this is the SAUNA", ["Sauna"]) == "Sauna"  # the area's own spelling


# --- goodbye --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["goodbye", "Goodbye!", "bye bye", "that's all", "That is all, thanks", "no thanks", "stop", "thanks hermes, goodbye", "never mind"],
)
def test_goodbyes(text):
    assert core.is_goodbye(text)


@pytest.mark.parametrize(
    "text", ["stop the music", "thanks", "what is goodbye in french", "bye the way what time is it", ""]
)
def test_not_goodbyes(text):
    assert not core.is_goodbye(text)


# --- speech ---------------------------------------------------------------------------------------


def test_speech_is_voice_clean_and_capped():
    assert "**" not in core.finalize_speech("**Hello** [there](http://x.com)\n\n- one\n- two")
    assert "Hermes" not in core.finalize_speech("Hermes Agent says hi")
    assert core.finalize_speech("Hermes says hi", unsay_name=False).startswith("Hermes")
    long = "Sentence one. " * 1000
    out = core.finalize_speech(long)
    assert len(out) <= 6000 and out.endswith(".")
    assert len(core.join_speech(["a" * 5000, "b" * 5000])) <= 6000
    assert core.join_speech(["one ", "", None, " two"]) == "one two"


def test_system_message_carries_style_room_time_speaker():
    msg = core.build_system_message("Be brief.", "Kitchen", "Now is noon.", "someone in the household")
    assert msg.splitlines() == [
        "Be brief.",
        "The speaker is in the Kitchen.",
        "Now is noon.",
        "Speaker: someone in the household.",
    ]
    assert "unknown" in core.build_system_message("", None, "", "x")


# --- rate limit -----------------------------------------------------------------------------------


def test_rate_limiter_is_per_key_and_sliding():
    now = [0.0]
    r = core.RateLimiter(limit=3, window=60, clock=lambda: now[0])
    assert [r.allow("a") for _ in range(4)] == [True, True, True, False]
    assert r.allow("b")
    now[0] = 61
    assert r.allow("a")


# --- turn queue -----------------------------------------------------------------------------------


def turn(speech, delay=0.0, log=None, fail=False):
    async def run():
        if log is not None:
            log.append(f"start {speech}")
        await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("secret detail")
        if log is not None:
            log.append(f"end {speech}")
        return core.TurnResult(speech, "hermes")

    return run


@aio
async def test_turns_run_in_order_and_answers_come_back_in_order():
    reg = core.TurnRegistry()
    q = reg.queue("s")
    log = []
    first = q.submit(turn("A", 0.05, log), "fb")
    second = q.submit(turn("B", 0.0, log), "fb")  # would finish first if it ran at once
    assert await q.wait(second, 2)
    assert log == ["start A", "end A", "start B", "end B"]
    assert first.done()
    assert q.take_ready() == ["A", "B"]
    assert q.take_ready() == []  # delivered once
    assert not q.has_work


@aio
async def test_pending_turn_keeps_running_and_is_delivered_next():
    reg = core.TurnRegistry()
    q = reg.queue("s")
    t = q.submit(turn("slow", 0.1), "fb")
    assert not await q.wait(t, 0.01)  # budget over: the caller answers "pending"
    assert q.busy and q.take_ready() == []
    assert await q.wait(None, 2)  # a resume waits for everything
    assert q.take_ready() == ["slow"]


@aio
async def test_cancelling_the_waiter_does_not_cancel_the_turn():
    reg = core.TurnRegistry()
    q = reg.queue("s")
    t = q.submit(turn("kept", 0.05), "fb")
    waiter = asyncio.ensure_future(q.wait(t, 5))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0.1)
    assert t.done() and q.take_ready() == ["kept"]


@aio
async def test_failure_and_timeout_say_the_fallback_without_error_text():
    reg = core.TurnRegistry(turn_timeout=0.05)
    q = reg.queue("s")
    q.submit(turn("x", fail=True), "sorry")
    q.submit(turn("y", delay=1), "too slow")
    assert await q.wait(None, 2)
    assert q.take_ready() == ["sorry", "too slow"]


@aio
async def test_a_failed_turn_does_not_block_the_next():
    reg = core.TurnRegistry()
    q = reg.queue("s")
    q.submit(turn("x", fail=True), "sorry")
    q.submit(turn("ok"), "sorry")
    assert await q.wait(None, 2)
    assert q.take_ready() == ["sorry", "ok"]


@aio
async def test_max_queue_length():
    reg = core.TurnRegistry(max_queue_len=2)
    q = reg.queue("s")
    q.submit(turn("a", 0.2), "fb")
    q.submit(turn("b", 0.2), "fb")
    with pytest.raises(core.QueueFull):
        q.submit(turn("c"), "fb")
    q.cancel()


@aio
async def test_global_concurrency_cap_across_queues():
    reg = core.TurnRegistry(max_running=2)
    running = peak = 0

    async def run():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        return core.TurnResult("ok", "hermes")

    turns = [reg.queue(f"s{i}").submit(run, "fb") for i in range(5)]
    await asyncio.wait(turns, timeout=3)
    assert all(t.done() for t in turns) and peak == 2


@aio
async def test_max_queues_and_idle_pruning():
    now = [0.0]
    reg = core.TurnRegistry(clock=lambda: now[0], max_queues=2, idle_ttl=100)
    reg.queue("a")
    reg.queue("b")
    with pytest.raises(core.QueueFull):
        reg.queue("c")
    now[0] = 61  # empty queues go after a minute
    reg.queue("c")
    assert len(reg) == 1
    # a queue holding an undelivered answer lives until idle_ttl
    q = reg.queue("d")
    t = q.submit(turn("answer"), "fb")
    await q.wait(t, 1)
    now[0] = 61 + 99
    reg.prune()
    assert len(reg) == 1 and q.has_work
    now[0] = 61 + 101
    reg.prune()
    assert len(reg) == 0


@aio
async def test_running_queue_is_never_pruned():
    now = [0.0]
    reg = core.TurnRegistry(clock=lambda: now[0], idle_ttl=1)
    q = reg.queue("s")
    q.submit(turn("slow", 0.2), "fb")
    now[0] = 10_000
    reg.prune()
    assert len(reg) == 1
    reg.cancel_all()
    assert len(reg) == 0
