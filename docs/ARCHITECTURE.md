# Architecture

The single source of truth for how the parts talk (interfaces v1; change them only with a new version), and why.

## Chain

```
Echo ──"Alexa, ask hermes …"──▶ Alexa custom skill (dev stage, en-US)
  ──▶ AWS Lambda `hermes-alexa` (python3.13, stdlib + boto3 only, default us-east-1)
  ──HTTPS + CF-Access service token + HA bearer──▶ Cloudflare named tunnel (default name `hermes-ha`)
  ──▶ Home Assistant container 127.0.0.1:8123 (Docker Desktop)
       └ custom component `hermes_voice` (vendored, patched) → view POST /api/hermes_voice/alexa/v1/turn
            ├ 1. room commands ("this is the kitchen")
            ├ 2. HA default agent (local intents, device-area aware)
            └ 3. voice proxy http://host.docker.internal:8642/v1/chat/completions (only chat completions, models, health)
                 ──▶ Hermes Agent profile `voice` api_server 127.0.0.1:8643 (tools: web, memory; decision 3 below)
```

## POST /api/hermes_voice/alexa/v1/turn

Auth: HA bearer token (non-admin user `alexa`). Content-Type `application/json`. Body ≤ 8 KiB.

Request:

| field | type | rules |
| --- | --- | --- |
| `text` | string | 1–1000 chars after strip; may be `""` only when `kind` is `resume` or `launch` |
| `kind` | enum | `utterance` (default), `resume` (user asked "what did you find" / "continue"), `launch` (skill opened with no question), `end` (session ended or stop: acknowledge only; undelivered answers stay queued for the next turn) |
| `alexa_device_id` | string | required, ≤ 512, `^[A-Za-z0-9._:-]+$` |
| `alexa_user_id` | string | required, same charset, ≤ 512 |
| `alexa_person_id` | string\|null | optional, same charset |
| `alexa_session_id` | string | required, same charset |
| `locale` | string | `^[a-z]{2}-[A-Z]{2}$`, default `en-US` |
| `budget_ms` | int | 1000–7000, default 5500: how long the view may wait before answering `pending` |
| `has_screen` | bool | default false |

Unknown fields → 400.

Success 200:

```json
{"status": "answered" | "pending",
 "speech": "plain text, already voice-clean, ≤ 6000 chars",
 "room": "Kitchen" | null,
 "source": "room" | "local" | "hermes" | "pending" | "system",
 "end_session": false}
```

- `pending`: Hermes is still working. `speech` is a short holding line. The turn keeps running in HA; the
  next request with the same conversation key returns its answer first (prepended), or on `kind=resume`
  waits up to `budget_ms` for it.
- `end_session: true` only for explicit goodbyes the view recognised.

Errors — one shape everywhere, any status ≥ 400:

```json
{"error": {"code": "invalid_request" | "unauthorized" | "rate_limited" | "upstream_unavailable" | "internal", "message": "safe text"}}
```

## Keys (computed in HA, never trusted from text)

- Room: Alexa device → HA device in the device registry (identifier `(hermes_voice, "alexa:" + sha256(alexa_device_id)[:24])`),
  created on first sight with no area; its area is the room. "this is the <room>" / "this device is in the <room>"
  sets it (creating the area if needed). Users can also change it in the HA UI.
- Speaker: `person:<sha256(person_id)[:16]>` when `alexa_person_id` is present, else `household`.
- Hermes `X-Hermes-Session-Id`: `alexa-<speaker>-<room slug or device hash>-<window>` where the window resets after
  30 idle minutes (follow-ups across Alexa sessions in the same room continue the same Hermes thread).
- Hermes `X-Hermes-Session-Key`: `household` (shared memory across the house; per-person facts still go to the
  shared profile memory, which is the point).
- Hermes system message: voice style + current room + local time + speaker label.
- The per-conversation turn queue (pending answers) is keyed by the Hermes session id.

## Lambda configuration

Env: `SKILL_ID`, `SSM_PREFIX` (default `/hermes-alexa/`), `LOG_LEVEL`. SSM parameters under the prefix:
`ha_url` (String, must be `https://`), `ha_token` (SecureString), `cf_access_client_id`, `cf_access_client_secret`
(SecureString, optional: sent only when present). Fetched once per cold start.

Timeouts: Lambda function timeout 10 s; HTTP socket timeout `budget_ms/1000 + 1.0` (urllib has one timeout, no separate
connect timeout); `budget_ms` = min(5500, Lambda time left - 4500 ms), at least 1000, so a slow cold start shrinks it; SSM calls
use a 1 s connect and 2 s read timeout. The Lambda sends `User-Agent: hermes-alexa-lambda/1` (Cloudflare blocks the default
Python one) and maps HA's HTTP status first (401, 403, 302 -> `unauthorized`, 413 -> `invalid_request`, 429 -> `rate_limited`,
5xx -> `upstream_unavailable`); a JSON `error.code` from the contract overrides it. `AMAZON.YesIntent` and `AMAZON.NoIntent` are
sent as the utterances `yes` and `no`. A progressive response ("One moment.") is sent from a timer thread if HA has not answered after 2.0 s.

## Decisions

1. **Lambda to tunnel to HA bridge to Hermes.** A stdlib-only Lambda (written from the behaviour of
   fabianosan/HomeAssistantAssistAWS, no code copied) calls an authenticated HA view in a vendored, patched `hermes_voice`
   (majorminorlabs, MIT). The view handles room commands, then HA local intents with the Echo's mapped HA device (area aware),
   then Hermes with per-room session ids and a shared household memory key. Slow Hermes turns return `pending` and are
   delivered on the next turn. HA's REST conversation API carries no device, so room awareness needs this own endpoint; the
   upstream AssistAWS shares one conversation id across all users and devices and has no timeouts.
2. **Auth.** A static non-admin HA token in SSM SecureString plus an optional Cloudflare Access service token, instead of HA
   account linking (single household, avoids exposing `/auth/authorize`). Tunnel ingress allows one path only. HA trusts
   `X-Forwarded-For` only from the cloudflared container's fixed IP.
3. **Dedicated, locked-down Hermes profile `voice` behind a route-allowlisting proxy.** Alexa turns run in their own Hermes
   profile (fresh, not a clone) with its own standalone gateway and api_server on `127.0.0.1:8643`. HA never holds that
   key: it calls `deploy/voice_proxy.py` on `:8642` with its own key, and the proxy forwards only `POST /v1/chat/completions`
   (body reduced to `model`, `messages`, `stream`, plus the two `X-Hermes-Session-*` headers), `GET /v1/models` and
   `GET /health`, with the upstream key. Both keys live in DPAPI.
   - Tools: `platform_toolsets.api_server: [web, memory, no_mcp]`, `platform_toolsets.cron: []`, every other toolset in
     `agent.disabled_toolsets`. `deploy.hermes_setup` asks Hermes' own resolver for the profile's enabled tools after start and
     stops the gateway if anything beyond web and memory is enabled.
   - Why a proxy and not only profile config: every Hermes api_server route accepts the same key, including jobs, sessions,
     responses and runs, and none can be switched off. `/v1/runs` can carry a client-supplied execution policy that replaces
     the configured toolsets, so the api_server key alone is terminal on the host whatever the profile says. Chat
     completions builds its agent from the platform toolsets only, so behind the proxy the reachable tools are web search,
     web extract and memory (checked against hermes-agent at commit f86276d; re-check after upgrades).
   - Why a standalone profile and not `/p/voice` multiplexing: a multiplexed listener has one key for every profile, so the key HA
     holds would also reach your main profile.
   - Residual risk: the upstream key sits in the voice profile's `.env` and in DPAPI for this Windows user. Containers can
     reach host loopback on Docker Desktop, so port 8643 is reachable from Docker but useless without that key.
   - Model: the voice profile keeps the default model with `reasoning_effort: none`. Pick a fast, non-reasoning one with
     `hermes -p voice model`.
4. **Invocation name "hermes".** One word, "Alexa, ask hermes ...". Amazon rejects one-word and person or deity names at
   certification, so the skill stays private in the development stage; rename it to two words before ever publishing, since
   the name cannot change after certification. A custom wake word is impossible: Echo wake words are a fixed on-device list.
5. **Hosting.** HA Container on Docker Desktop (127.0.0.1 only), a named Cloudflare tunnel in a container, logon tasks
   plus a watchdog.
