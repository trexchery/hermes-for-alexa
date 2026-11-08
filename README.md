# hermes-for-alexa

Talk to your own [Hermes Agent](https://github.com/NousResearch/hermes-agent) from any Amazon Echo in the house:
"Alexa, ask hermes what is the weather like in Lisbon". A Windows-hosted bridge from an Alexa custom skill, through AWS
Lambda and a Cloudflare tunnel, into Home Assistant and a locked-down Hermes profile. Native Alexa keeps working as before.

## How it works

```text
Echo ──"Alexa, ask hermes ..."──▶ Alexa custom skill (development stage)
  ──▶ AWS Lambda (stdlib + boto3)
  ──HTTPS, HA bearer token, optional Cloudflare Access token──▶ Cloudflare named tunnel (one path exposed)
  ──▶ Home Assistant in Docker, 127.0.0.1 only
       custom component hermes_voice (vendored, patched), POST /api/hermes_voice/alexa/v1/turn
         1. room commands ("this is the kitchen")
         2. Home Assistant's own intents for the room's devices ("turn on the light")
         3. voice proxy :8642 (chat completions only) ──▶ Hermes profile "voice" :8643 (web + memory tools only)
```

Everything on the PC starts at logon and is checked every 2 minutes by a watchdog. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Features

- Room awareness: each Echo is mapped to a Home Assistant device and area, so "turn on the light" means this room.
- Follow-ups continue the same Hermes thread per room and speaker, for 30 idle minutes. One shared household memory.
- Slow answers do not time out: Alexa gets a short holding line, and the answer arrives on the next turn or after "ask hermes what did you find".
- Least privilege end to end: a dedicated Hermes profile limited to web and memory, a route-allowlisting proxy, one exposed URL path, a Lambda role that can read only its own parameters.
- Self-healing: watchdog for Docker, Home Assistant, the tunnel, the Hermes gateway and the proxy.

## Alexa and Alexa+ limits

- A dev-stage skill needs its invocation name: always "Alexa, ask hermes ..." or "Alexa, open hermes". Name-free invocation and Alexa+ native routing are not available to personal skills.
- Only Echos registered to the Amazon developer account that owns the skill can use it.
- Alexa waits about 8 seconds for a skill. Slower Hermes answers are delivered on the next turn.
- Follow-Up Mode (no wake word) does not continue a custom skill session; multi-turn works only while the skill keeps the session open.
- Alexa does not expose rooms; you teach each Echo once ("Alexa, ask hermes this is the kitchen").
- Whether Alexa+ treats classic skill sessions identically is unverified: test on your device.
- The skill is not meant to be published: the one-word invocation name would fail certification.

## Requirements

Windows 11, Python 3.11+, Docker Desktop, [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/),
AWS CLI v2, Node.js and the `ask` CLI (`npm i -g ask-cli`), [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed, a domain on Cloudflare, an AWS account, an Amazon developer account.

## Quick install

```powershell
git clone https://github.com/trexchery/hermes-for-alexa; cd hermes-for-alexa
powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Hostname ha-voice.example.com -PrivacyUrl https://example.com/privacy
python scripts\verify_e2e.py
```

`install.ps1` is safe to re-run. At a missing login it prints the one command to run and exits; run it again afterwards.
The parameters are stored once, later runs need none.

### Steps only you can do

| Command | Why |
| --- | --- |
| `aws configure` (or `aws configure sso`) | AWS credentials with IAM, SSM, Lambda and CloudWatch Logs rights |
| `ask configure` | Amazon developer account (register at developer.amazon.com first) |
| `cloudflared tunnel login` | Pick the zone that holds your hostname |
| `hermes -p voice model` | Pick a small, fast model for the voice profile (after the installer creates it) |
| `python -m deploy.set_secret cf_api_token`, then `python -m deploy.cloudflare_access` | Optional: only the Lambda may reach the hostname (Cloudflare Access service token) |

## Configuration

Non-secret settings live in `%LOCALAPPDATA%\hermes-alexa\state.json`; secrets in `%LOCALAPPDATA%\hermes-alexa\secrets` (Windows DPAPI, current user).
Set with install.ps1 parameters or `python -m deploy.state set key=value`.

| Key | install.ps1 | Default | Meaning |
| --- | --- | --- | --- |
| `public_hostname` | `-Hostname` | required | public DNS name of the tunnel |
| `privacy_url` | `-PrivacyUrl` | required | privacy policy URL the skill manifest demands |
| `cf_tunnel_name` | `-TunnelName` | `hermes-ha` | Cloudflare tunnel name |
| `aws_region` | `-Region` | `us-east-1` | region of Lambda, SSM and IAM |
| `timezone` | `-TimeZone` | `UTC` | IANA zone for Home Assistant (written to `deploy/ha/.env`) |

`HERMES_ALEXA_HOME` overrides the base directory.

## Security model

- Secrets: DPAPI locally, SSM SecureString in AWS. Never in code, logs, URLs or argv. `.gitignore` covers `.env`, state and keys.
- The turn endpoint requires a non-admin Home Assistant token, validates every field, and returns one error shape. HA bans repeated bad tokens.
- Only one URL path is exposed through the tunnel; Home Assistant trusts forwarded headers solely from the cloudflared container.
- Hermes runs in its own profile with web and memory tools; a proxy forwards only chat completions, models and health. Rationale and residual risk: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- The Lambda accepts only its own skill id and its role reads only its own SSM path.

## Troubleshooting

Run `python scripts\verify_e2e.py`: it checks loopback, the public tunnel, the Lambda and an Alexa simulation, with latency per hop. Fixes per hop, recovery and token rotation: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Development

`python -m pip install -e ".[test]"` then `python -m pytest`. Home Assistant component tests: `ha/tests/run.sh` (Docker). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

- [majorminorlabs/hermes-voice](https://github.com/majorminorlabs/hermes-voice) (MIT): vendored and patched in `ha/custom_components/hermes_voice` (licence in `LICENSE.upstream`).
- Inspired by [fabianosan/HomeAssistantAssistAWS](https://github.com/fabianosan/HomeAssistantAssistAWS) (GPL-3.0); no code was copied.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.

MIT licensed, see [LICENSE](LICENSE).
