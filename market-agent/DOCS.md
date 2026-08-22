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

## Connection status

A second pill shows whether the add-on's own connection to xWeb's
SignalR hub is actually up right now — green "xWeb: connected", amber
"xWeb: connecting…"/"xWeb: reconnecting…", or amber "xWeb: disconnected"
if even the automatic reconnect has dropped and a fresh connection is
being rebuilt. This says the pipe to xWeb is open, not that xWeb's own
loop is still ticking on schedule.

Two more lines say what the loop is actually doing: **"Last update"**
(how long ago the most recent tick arrived) and **"Next check"** (when
the next one's expected). "Next check" is exact only when the market's
closed — xWeb tells us the precise reopen time and it's shown as
"Market closed — reopens in Xh Ym". Otherwise it's labelled
"(estimated)": this add-on isn't told xWeb's configured poll interval,
so it infers one from the gap between the last two ticks. If the
connection pill is green but "Last update" is far older than "Next
check" ever predicted, that's more likely xWeb's loop being stuck than
a connectivity problem.

## Saxo login status

A colored pill next to the symbol shows whether xWeb currently has a
valid Saxo session — green "Saxo: connected", red "Saxo: login required",
or grey "Saxo: unknown" before the first tick arrives. The pill is a
link straight to Saxo's login flow, defaulting to
`http://<xweb_host>/saxo/login` — LAN-only, but zero configuration
needed. If you want that link (and the one in push notifications) to
still work when tapped away from home, set `saxo_login_url` below to
your own WAN-reachable hostname for it — that value lives in your own
Supervisor config, not in this public repo, so it never puts a domain
name in source.

## Notifications

When a tick's threshold condition flips from not-met to met, a push
notification fires via `notify_service` (below) — once per transition,
not repeated every tick while it stays true.

Saxo login state gets the same treatment: a push fires once when a tick
shows login is required, and once more when it resolves — covering both
the background loop's routine ticks and the manual **Run real analysis
now** button, not just the button.

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
| `saxo_login_url` | *(empty)* | Overrides the Saxo login link (pill + notifications) with your own WAN-reachable URL. Defaults to `http://<xweb_host>/saxo/login` (LAN-only) if left empty |

## Logs

Live: the add-on's **Log** tab, or `ha addons logs market-agent`.

## Behaviour worth knowing

**Nothing secret is in this add-on.** No keys, no tokens — auth to Home
Assistant is via the Supervisor's own proxy, and xWeb itself has no auth
on the LAN-only endpoints this add-on talks to. This repository is public.
