# EasyPz Market Agent

A live view of the Workflow Service's `MarketAgentBackgroundService` loop:
a price chart, a table of recent ticks, and a button to trigger a real
(billed) analysis on demand.

## How it stays live

A background thread holds one persistent connection to the Workflow
Service's SignalR hub (`/streamHub`, topic `marketagent.preview`) open
for the life of the container — no polling. Every tick the Workflow
Service's own loop produces (by default every 5 minutes, free) shows up
here as it happens. A dropped connection (a Workflow Service restart,
network blip) reconnects on its own.

## Connection status

A second pill shows whether the add-on's own connection to the Workflow
Service's SignalR hub is actually up right now — green "Workflow
Service: connected", amber "Workflow Service: connecting…"/
"Workflow Service: reconnecting…", or amber "Workflow Service:
disconnected" if even the automatic reconnect has dropped and a fresh
connection is being rebuilt. This says the pipe to it is open, not that
its own loop is still ticking on schedule.

Two more lines say what the loop is actually doing: **"Last update"**
(how long ago the most recent tick arrived) and **"Next check"** (when
the next one's expected). "Next check" is exact only when the market's
closed — the Workflow Service tells us the precise reopen time and it's
shown as "Market closed — reopens in Xh Ym". Otherwise it's labelled
"(estimated)": this add-on isn't told the configured poll interval, so
it infers one from the gap between the last two ticks. If the connection
pill is green but "Last update" is far older than "Next check" ever
predicted, that's more likely the loop being stuck than a connectivity
problem.

## Saxo login status

A colored pill next to the symbol shows whether the Workflow Service
currently has a valid Saxo session — green "Saxo: connected", red "Saxo:
login required", or grey "Saxo: unknown" before the first tick arrives.

Clicking the pill doesn't send your browser to the Workflow Service
directly — it hits this add-on's own `/saxo-login` route, which asks the
Workflow Service server-side (from HAOS, a normal LAN device) where the
login flow redirects to, and relays that straight to your browser. Two
reasons: your browser never makes a cross-origin request to a LAN IP
(which is exactly what trips Chrome's Local Network Access prompt when
viewing this panel through a public hostname), and it works identically
whether you're on the LAN or not, since it's riding the same ingress
tunnel already getting you to this panel.

Push notifications are different — tapped from outside any HA page
entirely, so they still need a real, standalone URL rather than a
relative path. That link defaults to `http://<workflow_service_host>/saxo/login`
(LAN-only, zero configuration needed). If you want *that* link to work
when tapped away from home, set `saxo_login_url` below to your own
WAN-reachable hostname for it — that value lives in your own Supervisor
config, not in this public repo, so it never puts a domain name in
source.

## Notifications

When a tick's threshold condition flips from not-met to met, a push
notification fires via `notify_service` (below) — once per transition,
not repeated every tick while it stays true.

Saxo login state gets the same treatment: a push fires once when a tick
shows login is required, and once more when it resolves — covering both
the background loop's routine ticks and the manual **Run real analysis
now** button, not just the button. The login-required push is tappable
— it opens the login flow directly rather than just opening Home
Assistant itself (that's the difference between putting a URL in the
notification's `data.url` field, which makes it a real tap target, and
only mentioning it in the message text, which doesn't).

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
| `workflow_service_host` | `192.168.0.201` | LAN address of the Workflow Service |
| `market_agent_symbol` | `XAGUSD` | Symbol this panel tracks |
| `notify_service` | *(empty)* | HA notify service name for pushes — pushes are silently skipped until this is set |
| `saxo_login_url` | *(empty)* | Overrides the Saxo login link (pill + notifications) with your own WAN-reachable URL. Defaults to `http://<workflow_service_host>/saxo/login` (LAN-only) if left empty |

## Logs

Live: the add-on's **Log** tab, or `ha addons logs market-agent`.

## Behaviour worth knowing

**Nothing secret is in this add-on.** No keys, no tokens — auth to Home
Assistant is via the Supervisor's own proxy, and the Workflow Service has
no auth on the LAN-only endpoints this add-on talks to. This repository
is public.
