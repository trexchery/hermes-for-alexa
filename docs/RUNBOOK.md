# Runbook

Interfaces and decisions are in `docs/ARCHITECTURE.md`. This file covers running it on the Windows PC.

## Architecture

```
Echo -> Alexa skill "hermes" (dev stage) -> Lambda hermes-alexa (your aws_region)
     -> https://<public_hostname> (Cloudflare tunnel <cf_tunnel_name>, optional Access service token)
     -> Home Assistant container 127.0.0.1:8123 (custom component hermes_voice)
     -> voice proxy http://host.docker.internal:8642/v1 (deploy/voice_proxy.py: chat completions, models, health only)
     -> Hermes profile "voice" api_server 127.0.0.1:8643 (own gateway, web + memory tools only, see ARCHITECTURE decision 3)
```

Everything on the PC starts at logon and is watched every 2 minutes.

| Piece | Where it lives | Started by |
| --- | --- | --- |
| Home Assistant | Docker Desktop, `deploy/ha/compose.yaml`, `restart: unless-stopped` | Docker Desktop AutoStart |
| Cloudflare tunnel | `%USERPROFILE%\.hermes-tunnel\config.yml`, only `^/api/hermes_voice/alexa/v1/turn$` is exposed | container `hermes-cloudflared` (`deploy/ha/compose.yaml`, fixed IP 172.30.77.2, the only proxy HA trusts; restart policy + watchdog) |
| Hermes voice gateway | `%LOCALAPPDATA%\hermes\profiles\voice`, log `logs\` there | watchdog: `hermes -p voice gateway start` (detached, hidden) |
| Voice proxy | `deploy/voice_proxy.py`, log `%LOCALAPPDATA%\hermes-alexa\voice-proxy.log` | task `Hermes-Voice-Proxy` (restart loop) |
| Watchdog | `deploy/windows/watchdog.ps1`, log `%LOCALAPPDATA%\hermes-alexa\watchdog.log` | task `Hermes-Watchdog` |
| Secrets | `%LOCALAPPDATA%\hermes-alexa\secrets\*.bin` (Windows DPAPI, current user only) | `deploy/secrets.py` |
| Settings and state (hostname, region, skill id, Lambda ARN) | `%LOCALAPPDATA%\hermes-alexa\state.json` | deploy scripts |

## Install

```powershell
cd <your clone>
powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Hostname ha-voice.example.com -PrivacyUrl https://example.com/privacy
```

The parameters are stored in `state.json` once (optional: `-TunnelName`, `-Region`, `-TimeZone`). It runs 13 numbered steps, each safe to repeat. At a missing login it prints the one command to run and exits
with code 2; run `install.ps1` again and it continues. Then check the chain:

```powershell
python scripts\verify_e2e.py        # loopback, public tunnel, Lambda, real Alexa simulation, with latency per hop
```

## Steps only you can do

| When | Command | Why |
| --- | --- | --- |
| Once | `cloudflared tunnel login` | Pick the zone that holds your hostname. Creates `cert.pem`. |
| Once | `aws configure` (or `aws configure sso`) | AWS account credentials. The Lambda needs IAM, SSM and Lambda rights. |
| Once | `ask configure` | Amazon developer account login in the browser. |
| Optional | `python -m deploy.set_secret cf_api_token`, then `python -m deploy.cloudflare_access` | Cloudflare Access service token so only the Lambda can reach the hostname. Token needs Access Apps and Policies Edit, Access Service Tokens Edit, Zone Read. Skipped cleanly without it. |
| Optional | Sysinternals Autologon | Without auto-logon nothing starts until you sign in (Docker Desktop and the tasks are logon-based). |
| After install | Alexa app: skill "Hermes" is enabled for the developer account | Dev-stage skills only work on Echos registered to the same Amazon account. |
| Per room | Say: "Alexa, ask hermes this is the kitchen" | Teaches the room of that Echo (stored as an HA area). |

## What the scripts change (all with backups)

- Hermes profile `voice` (created with `hermes profile create voice --no-alias --no-skills`, a fresh profile, not a clone).
  Its `.env`: `API_SERVER_ENABLED/KEY/HOST/PORT` (127.0.0.1:8643) plus `OPENROUTER_API_KEY` and `FIRECRAWL_API_KEY/URL` copied
  from the main `.env` when present. Its `config.yaml`: `gateway.standalone: true`, `gateway.api_server.max_concurrent_runs: 6`,
  `display.platforms.api_server.interim_assistant_messages: false`, `platform_toolsets.api_server: [web, memory, no_mcp]`,
  `platform_toolsets.cron: []`, `agent.disabled_toolsets` (every other toolset), `agent.reasoning_effort: none`,
  `model.reasoning_effort: none`. `config.yaml` backups are `*.bak-<timestamp>` beside it; the `.env` is regenerated and never backed up (keys). The main profile is never edited.
- DPAPI: `hermes_api_key` (HA -> proxy) and `hermes_voice_upstream_key` (proxy -> voice gateway). HA never holds the second one.
- `%APPDATA%\Docker\settings-store.json`: `AutoStart: true`.
- Power plan: no sleep or hibernate on AC.
- Bind address: the installer tests whether a container reaches host loopback. If yes, the voice proxy binds `127.0.0.1:8642`. If not, it binds the Docker/WSL vEthernet adapter IP (never `0.0.0.0`) and needs a
  firewall rule `hermes-alexa-8642` limited to `172.16.0.0/12,192.168.65.0/24` (needs an elevated shell once; the exact command is printed).

## Recovery

| Symptom | Check | Fix |
| --- | --- | --- |
| Nothing answers after a reboot | Are you signed in to Windows? | Sign in, or set up auto-logon. Tasks start 30 s after logon. |
| `docker ps` fails | watchdog.log "docker down" | `Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'` |
| HA unhealthy | `docker logs --tail 50 homeassistant` | `docker restart homeassistant` (the watchdog does this after 3 bad checks) |
| Tunnel down | `curl http://127.0.0.1:20251/ready`, `docker logs --tail 50 hermes-cloudflared` | `docker restart hermes-cloudflared` (the watchdog does this) |
| Voice gateway not answering | `curl http://127.0.0.1:8643/health`, `%LOCALAPPDATA%\hermes\profiles\voice\logs` | `& "$env:LOCALAPPDATA\hermes\bin\hermes.cmd" -p voice gateway restart` (the watchdog starts it, and restarts it after 2 bad checks) |
| Proxy not answering (HA says Hermes unavailable) | `curl http://127.0.0.1:8642/health`, `voice-proxy.log` | `Start-ScheduledTask Hermes-Voice-Proxy` (the watchdog does this) |
| Voice gets more tools than web and memory | `python -m deploy.hermes_setup` (asks Hermes' own toolset resolver for the voice profile; if more, disables the api_server and stops the gateway) | add the new toolset to `DENIED_TOOLSETS` in `deploy/hermes_setup.py` and rerun |
| Lambda errors | CloudWatch group `/aws/lambda/hermes-alexa` (14 days) | `python -m deploy.aws_deploy` |
| Component changed | | `docker restart homeassistant` (the directory is mounted read-only from the repo) |
| Update HA | | Change the tag in `deploy/ha/compose.yaml`, then `docker compose -f deploy\ha\compose.yaml up -d` |
| Rebuild everything | | Run `install.ps1` again. It never recreates what exists. HA data is in the volume `ha_config`. |

Backup of HA: `docker run --rm -v ha_config:/c -v C:\backup:/b alpine tar czf /b/ha.tgz -C /c .`

## Token rotation

- HA tokens (365 days): delete the DPAPI file, rerun bootstrap, push to AWS:
  `del %LOCALAPPDATA%\hermes-alexa\secrets\ha_alexa_token.bin; python -m deploy.bootstrap_ha; python -m deploy.aws_deploy`.
  Revoke the old one in HA: Profile > Security > Long-lived tokens (as user `alexa`, or via the `auth/delete_refresh_token` websocket command).
  An expired or wrong token makes HA ip-ban the Lambda's egress IPs after 5 failures (403 for everyone behind them). After rotating:
  `docker exec homeassistant rm -f /config/ip_bans.yaml; docker restart homeassistant`. Keep the Cloudflare Access step on so only the Lambda reaches HA.
- HA's Hermes key (`hermes_api_key`): `del ...\secrets\hermes_api_key.bin; python -m deploy.hermes_setup`, restart task
  `Hermes-Voice-Proxy`, then remove the `hermes_voice` entry in HA (Settings > Devices & services) and rerun `python -m deploy.bootstrap_ha`.
- Gateway key (`hermes_voice_upstream_key`): `del ...\secrets\hermes_voice_upstream_key.bin; python -m deploy.hermes_setup` (restarts the voice gateway), then restart task `Hermes-Voice-Proxy`.
- Cloudflare service token (1 year): `del ...\cf_access_client_id.bin`, rerun `python -m deploy.cloudflare_access` and `python -m deploy.aws_deploy`.
  Delete the old token in Zero Trust > Access > Service Auth.

## Alexa and Alexa+ limits

See the [README](../README.md#alexa-and-alexa-limits).

## Open items

- Voice model: pick a fast, non-reasoning model with `hermes -p voice model`. `agent.api_max_retries: 1` and
  `agent.auto_recovery_cycles: 0` also help latency and can be set in the voice profile alone.
- Skill privacy policy URL: required by the manifest, unused for a private skill. Set once with `-PrivacyUrl` or `--privacy-url`.

## Troubleshooting

- `verify_e2e.py` hop (a) fails: HA or the `alexa` token. `docker logs homeassistant`; rerun `python -m deploy.bootstrap_ha`.
- Hop (b) fails but (a) passes: tunnel or Access. `curl http://127.0.0.1:20251/ready`; 403 means the service token headers are missing or wrong.
- Hop (c) fails: SSM parameters or `SKILL_ID` env (`aws lambda get-function-configuration --function-name hermes-alexa --region <aws_region>`).
- Hop (d) fails: skill not enabled in development, or the endpoint ARN/permission missing: rerun `skill_deploy` then `aws_deploy`.
- HA log "received X-Forwarded-For from untrusted proxy": fix `trusted_proxies` in `deploy/bootstrap_ha.py` and rerun it.

## Notes

- HA 2026.11 keeps `http:` settings in `.storage/http`, imports YAML once, and reverts any change not promoted within
  5 minutes. `deploy/bootstrap_ha.py` stages the reverse-proxy settings over the admin websocket and promotes them.
- The tunnel's metrics port is 20251 (published on loopback) so it does not clash with another cloudflared on the default 20241.
