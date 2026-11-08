# Home Assistant side: `hermes_voice` with the Alexa bridge

`custom_components/hermes_voice` is [majorminorlabs/hermes-voice](https://github.com/majorminorlabs/hermes-voice)
(MIT, commit `0c3eea3`, licence in `LICENSE.upstream`), patched for this project. It does two jobs:

1. A Home Assistant conversation agent backed by a Hermes Agent profile (upstream's feature, kept).
2. The **Alexa bridge**: the view the Lambda calls (`docs/ARCHITECTURE.md`). New here.

Needs Home Assistant 2026.10 or newer (tested on 2026.10.4, imports checked on 2026.11.0, see Tests).

## Install

1. Copy `custom_components/hermes_voice` into `<HA config>/custom_components/` and restart Home Assistant.
   (With the Docker setup in `deploy/`, mount `ha/custom_components` there.)
2. Settings > Devices & services > Add integration > **Hermes Voice**. Enter the gateway URL, the profile's
   `API_SERVER_KEY` and a display name. The default `http://host.docker.internal:8642/v1` reaches the Hermes
   gateway on the Windows host from the container.
3. Create a **non-admin** HA user `alexa` and a long-lived access token for it. That token is the Lambda's `ha_token`.
   It can call the bridge and nothing else it should not.

The gateway URL must be `https://`, or `http://` to `localhost`, a loopback address, `host.docker.internal` or a private
IP (10/8, 172.16/12, 192.168/16, fc00::/7, link-local). Anything else is refused in the config flow, and an existing
entry with such a URL fails to load (see S4 below).

## Options

Settings > Devices & services > Hermes Voice > Configure.

| Option | Default | Meaning |
| --- | --- | --- |
| `alexa_bridge` | off | This entry answers the Alexa view. If no entry is flagged, the first loaded entry does. |
| `session_mode` | `per_conversation` | Assist sessions only (not the Alexa bridge): one Hermes session per HA conversation, or per device with an idle window. |
| `idle_reset_minutes` | 30 | Per-device windows idle for longer than this are dropped (D3). |
| `user_key` | empty | Sent as `X-Hermes-Session-Key` for Assist turns. `{user_id}`, `{device_id}`. Alexa turns always send `household`. |
| `voice_cleanup`, `avoid_name`, `voice_instruction`, `send_time` | on, on, built-in, on | Speech clean-up and the system message. The Alexa bridge uses them too. |
| `fallback_message` | "I can't reach my brain..." | Spoken when Hermes fails or times out. |
| `timeout` | 120 s | Total limit for one Hermes turn (was 30 s upstream). Connect is 5 s and 30 s of silence is fatal (`client.py`). |
| `stream`, `working_phrase` | on, "One moment." | Assist streaming only. |


## The Alexa bridge endpoint

`POST /api/hermes_voice/alexa/v1/turn` with `Authorization: Bearer <token of user alexa>` and `Content-Type: application/json`.
The request and response are defined in `docs/ARCHITECTURE.md`; this is how the view behaves.

- **Checks.** Non-JSON, wrong content type, bodies over 8 KiB (413), unknown fields and bad values are refused with the
  contract's error shape `{"error": {"code", "message"}}` and never contain exception text. Per HA user about 30
  requests a minute, then 429 `rate_limited`. 503 `upstream_unavailable` when no entry is loaded. A missing or bad
  token is answered by Home Assistant itself (401, plain text, not our shape).
- **Room.** Each Alexa device becomes an HA device with identifier `(hermes_voice, "alexa:" + sha256(alexa_device_id)[:24])`,
  attached to the config entry, with no area at first. Its area is its room. "This is the kitchen", "this device is in the
  living room", "you are in the office" set it, and create the area if it is missing. Everyday phrases ("this is the ...")
  only count when the name looks like a room or is an existing area, so "this is the best pizza" still goes to Hermes.
  You can also change the area in the HA UI. At most 50 Alexa devices per entry.
- **Order for an utterance.** Goodbye ("goodbye", "that's all", "no thanks", "stop": answered here, `end_session: true`),
  then the room command, then Home Assistant's own agent (`conversation.async_converse`, agent
  `conversation.home_assistant`, with the mapped device so area-aware commands work). Its answer is used unless it is an
  error with code `no_intent_match` or `no_valid_targets`. Otherwise Hermes answers.
- **Hermes.** Through the integration's client, with `X-Hermes-Session-Id: alexa-<speaker>-<room slug or device hash>-<window>`
  (the window resets after 30 idle minutes), `X-Hermes-Session-Key: household`, and a system message with the voice style,
  room, local time and speaker. Replies are voice-cleaned and capped at 6000 characters.
- **Slow answers.** Turns run in the background, one at a time per speaker and device, so a pending answer survives a room change or a 30 minute pause (at most 3 queued, 3 running across
  the house, 64 conversations, 5 minutes per turn). If Hermes has not answered within `budget_ms` the view answers
  `pending` with a holding line and the turn carries on. The next request for the same session gets finished answers
  first, in order, once; a request with nothing left to say answers "I already gave you that answer." `kind=resume` waits up to `budget_ms` for them. `kind=end` only acknowledges.
  Pending answers live in memory: a Home Assistant restart drops them. An options save (entry reload) keeps running turns; only
  removing the entry cancels them. A voice room command creates an area only while Home Assistant has fewer than 30 areas, and
  a local intent that ran with empty speech answers "Done." instead of being repeated to Hermes.

## Upstream defects fixed

File and line numbers are in upstream `0c3eea3`.

| Id | Upstream defect | Fix |
| --- | --- | --- |
| S1 | `screen.py:244-249` with `client.py:166-168`: the API key went in clear text to an `http://` activity sidecar. | The screen feature is deleted, and so is `HermesClient.async_get_json`, which took arbitrary URLs. |
| S2 | `screen.py:439-441`, `537-551`: websocket commands not admin-only (live transcripts, jobs, `stop_timer`). | Deleted with the screen. |
| S3 | `screen.py:308`, `367`: unauthenticated page and static files. | Deleted with the screen. |
| S4 | `config_flow.py:73`, `122`: TLS never required, `http://` the default. | `rules.base_url_allowed`: https, or http to loopback, `host.docker.internal` or a private IP. Checked in the config flow (`insecure_url`) and at setup (`ConfigEntryError`). |
| S5 | `conversation.py:66-68`: missing user or device became `anonymous` / `nodevice`, one shared memory scope. | The fallback is `conv-<conversation id>`, a scope of its own (`rules.render_user_key`). |
| D2 | `session.py:28-30`: `ha-<conversation_id>`, so anyone who learns an id could continue another user's Hermes session. | Ids are `ha-u<sha256(user id)[:12]>-<conversation id>`, and per-device ids carry the same user prefix. |
| D3 | `session.py:96-97`: device windows were never pruned. | Windows idle for longer than `idle_reset_minutes` are dropped on every turn (`rules.prune_windows`). |
| D4 | `client.py:225`, `311-315`: one total timeout over the whole stream; nothing closed the request when HA gave up. | Connect 5 s, silence 30 s (streams; Hermes sends keepalives every 10 s) and total are separate. The total default is 120 s. The stream is wrapped in `aclosing`, so a cancelled or abandoned turn closes its HTTP request at once. Hermes cannot be told to stop the run itself. |
| D5 | `client.py:164`, `275-277` (issue #9): malformed gateway payloads raised raw exceptions. | Non-dict or odd JSON, `choices` of the wrong type, non-string content, over-long stream lines and odd error bodies all raise `HermesError`. |

Not changed: D1 (the stock `per_device` mode only sees what HA passes; the Alexa bridge has its own keys) and D6
(Piper-style chunking, harmless here). Note that HA 2026.11's REST `/api/conversation/process` does accept `device_id`.

## Tests

- `tests/ha/` in the repo root: pure Python (rules, Alexa logic, turn queue, client against a fake session). Plain
  `python -m pytest tests/ha`, no Home Assistant needed, part of CI.
- `ha/tests/`: view, registries, config flow, options flow, sessions and the conversation entity inside Home Assistant.
  `ha/tests/run.sh` runs them in a `python:3.14-slim` container (Home Assistant 2026.11 needs Python 3.14) with
  `pytest-homeassistant-custom-component==0.13.375`. That is the newest release and it pins Home Assistant 2026.10.4, so the suite
  runs on 2026.10.4. Installing 2026.11.0 over it fails inside the plugin (`_clear_logger_overwrites`). On 2026.11.0
  only the imports and the registry and conversation APIs the bridge uses were checked. Re-run `run.sh` with
  `PYTEST_HA=<new version> HA_VERSION=2026.11.0` when a 2026.11 release of the plugin appears.
