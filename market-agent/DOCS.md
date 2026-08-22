# EasyPz Market Agent

A live view of xWeb's `MarketAgentBackgroundService` loop: a price chart,
a table of recent ticks, and a button to trigger a real (billed) analysis
on demand.

## How it stays live

A background thread holds one persistent connection to xWeb's SignalR hub
(`/streamHub`, topic `marketagent.preview`) open for the life of the
container — no polling. Every tick xWeb's own loop produces (by default
every 5 minutes, free — see xWeb's `CLAUDE.md`) shows up here as it
happens. A dropped connection (xWeb pod restart, network blip) reconnects
on its own.

## Notifications

When a tick's threshold condition flips from not-met to met, a push
notification fires via `notify_service` (below) — once per transition,
not repeated every tick while it stays true.

If the **Run real analysis now** button hits a Saxo 401 (token not logged
in), it also notifies, with a login link to `kumuruku.com/saxo/login` —
deliberately the WAN hostname, so the link works even away from home.
This currently only covers the manual button: a token expiring silently
between clicks during the background loop's own routine ticks isn't
detected or notified on yet.

Notifications need no secret or long-lived token — this add-on has its
own Supervisor, which proxies the Home Assistant API automatically
(`homeassistant_api: true` in `config.yaml`). Just set `notify_service`
below to the exact service name (HA Developer Tools → **Actions** →
search `notify`) and pushes start working. Until it's set, pushes are
silently skipped — everything else still works.

## History

`/share/market-agent/log.jsonl`, bounded to the most recent 500 ticks.

## Options

| Option | Default | Purpose |
|---|---|---|
| `xweb_host` | `192.168.0.201` | LAN address of xWeb's `xweb-lan` Service |
| `market_agent_symbol` | `XAGUSD` | Symbol this panel tracks |
| `notify_service` | *(empty)* | HA notify service name for pushes — pushes are silently skipped until this is set |

## Logs

Live: the add-on's **Log** tab, or `ha addons logs market-agent`.

## Behaviour worth knowing

**Nothing secret is in this add-on.** No keys, no tokens — auth to Home
Assistant is via the Supervisor's own proxy, and xWeb itself has no auth
on the LAN-only endpoints this add-on talks to. This repository is public.
