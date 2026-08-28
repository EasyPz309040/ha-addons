# EasyPz Market Agent

A live view of the Workflow Service's `MarketAgentBackgroundService` loop:
a price chart, the AI analysis, and the workflow history, in three sections.

## Panel layout

The panel is three cards, top to bottom:

1. **Status** — symbol, connection/auth pills, last update/next check, the
   price chart, and a compact side-by-side summary (**Price move** /
   **Volatility** / **Volume**) of the *last* check — the same measure
   cards described under "Workflow detail" below, just for whichever check
   just happened rather than one you clicked into. The chart's time axis
   labels are the candles' own timestamps (real price data time, from
   the upstream data source), not when the add-on happened to receive the
   check that carried them. When the market's closed, the data source has
   nothing newer to hand back, so the chart keeps showing the last real
   session's candles rather than going blank — the caption below the
   chart adds the date (not just the time) and a bold "market is likely
   closed" note whenever those candles aren't from today, so stale data
   is never mistaken for live price action.
2. **AI Analysis** — the **Run AI Trend Analysis** button (a real, billed Claude
   call, on demand) and, underneath it, the most recent *billed* run's
   question and answer — this shows up here automatically the moment one
   happens, whether triggered by the button or by the background loop's
   own threshold check, not just after you click it yourself.
3. **Workflow History** — every check the Workflow Service's loop has
   produced recently, one row each. Click a row (the little chip on its
   time) for that check's full **Workflow detail**.

## How it stays live

A background thread holds one persistent connection to the Workflow
Service's SignalR hub (`/streamHub`, topic `marketagent.preview`) open
for the life of the container — no polling. Every check the Workflow
Service's own loop produces (by default every 5 minutes, free) shows up
here as it happens. A dropped connection (a Workflow Service restart,
network blip) reconnects on its own.

A second, independent watchdog thread catches the case where the
connection doesn't cleanly drop but goes quietly deaf instead — the
underlying socket stays open, the pill still says "connected", but no
further check ever arrives (observed for real when the Workflow Service
itself was redeployed out from under an already-open connection). If a
full minute passes with the pill saying "connected" but no new check in
the last 25 — a generous multiple of the normal 5-minute cadence — the
watchdog force-closes the connection itself, which hands it straight
back to the same reconnect logic a real network failure would trigger.
Nothing to configure; this runs automatically alongside the main
connection.

## Connection status

A second pill shows whether the add-on's own connection to the Workflow
Service's SignalR hub is actually up right now — green "Workflow
Service: connected", amber "Workflow Service: connecting…"/
"Workflow Service: reconnecting…", or amber "Workflow Service:
disconnected" if even the automatic reconnect has dropped and a fresh
connection is being rebuilt. This says the pipe to it is open, not that
its own loop is still checking on schedule.

Two more lines say what the loop is actually doing: **"Last update"**
(how long ago the most recent check arrived) and **"Next check"** (when
the next one's expected). "Next check" is exact only when the market's
closed — the Workflow Service tells us the precise reopen time and it's
shown as "Market closed — reopens in Xh Ym". Otherwise it's labelled
"(estimated)": this add-on isn't told the configured poll interval, so
it infers one from the gap between the last two checks. If the connection
pill is green but "Last update" is far older than "Next check" ever
predicted, that's more likely the loop being stuck than a connectivity
problem.

## Login status

A colored pill next to the symbol shows whether the Workflow Service
currently has a valid upstream session — green "Auth: connected", red
"Auth: login required", or grey "Auth: unknown" before the first check
arrives.

This is pushed from its own dedicated SignalR topic, separate from
`marketagent.preview` — it fires the moment a login or token refresh
actually happens on the Workflow Service side, not on the preview loop's
5-minute cadence. That's what makes the pill (and the login-required/
resolved push notification) react within moments of logging in rather
than waiting for the next scheduled check. Older Workflow Service
versions without this topic fall back to inferring the pill from the
latest preview check's own status — laggier, but still correct.

The pill links straight to the same real, standalone login URL used for
push notifications (see below) — not a relative path through this
add-on's own ingress route. That used to be relayed server-side instead,
which worked from a browser tab already logged into this same HA
instance, but broke when tapped from inside the HA companion app: the
pill opens with `target="_blank"`, which is what makes the app hand the
tap off to the system browser rather than navigating its own embedded
view — but that external context doesn't carry the ingress session's own
auth cookie, so the relative URL hit Home Assistant's own login instead
of ever reaching the real destination. A real absolute URL has no
ingress session to lose, so it now works the same everywhere: tapped
from a notification, opened in a browser, or opened from inside the app.

Push notifications need a real, standalone URL too — tapped from outside
any HA page entirely, so a relative path wouldn't resolve to anything.
That link defaults to the Workflow Service's own login route on
`workflow_service_host` (LAN-only, zero configuration needed). If you
want *that* link to work when tapped away from home, set
`auth_login_url` below to your own WAN-reachable hostname for it — that
value lives in your own Supervisor config, not in this public repo, so
it never puts a domain name in source.

## Notifications

When a check's threshold condition flips from not-met to met, a push
notification fires via `notify_service` (below) — once per transition,
not repeated every check while it stays true.

Login state gets the same treatment: a push fires once when a check
shows login is required, and once more when it resolves — covering both
the background loop's routine checks and the manual **Run AI Trend
Analysis** button, not just the button. The login-required push is tappable
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

## Trigger settings

`price_move_threshold_percent`, `volatility_threshold_percent`, and
`system_prompt` (below) let you adjust the background loop's trigger
thresholds and the Claude system prompt without touching the Workflow
Service directly. Leave any of them blank to leave the Workflow
Service's own value untouched.

This add-on pushes whatever's configured to the Workflow Service on
every SignalR connect or reconnect — not just once at startup. That's
deliberate, not incidental: a Workflow Service redeploy wipes its own
config the same way it wipes everything else that isn't on a persistent
volume, but that redeploy also drops this add-on's connection, so the
very next reconnect re-pushes the configured values automatically. A HA
config change reaches the Workflow Service the same way — saving add-on
options restarts it, and startup is itself a first connect. One
mechanism covers both cases; there's nothing to click and nothing to
remember.

There is currently no way to adjust `volume_multiplier` this way, because
there's no longer a multiplier to adjust — Volume was removed from the
trigger entirely, see "Workflow detail" below.

## Workflow detail

Click any row in **Workflow History** for that check's full data — every
`TriggerMetrics` field, not just the four summarized in the table, plus
that check's own candle chart. If it was a real (billed) run, the Claude
question/answer and (once the Workflow Service reports it) token counts
and an estimated cost show up here too. Nothing new is collected for
this — the full payload was already being persisted per check, this just
exposes it.

**Three measure cards up top (Price move / Volatility / Volume), side by
side.** Price move and Volatility each show the measured value, the
configured threshold, whether that check was **over threshold** or
**under threshold** (straight from the Workflow Service's own `Reasons`,
not re-derived here), and the **baseline** value that measurement was
actually taken against. Both are baseline-relative — % change of the
current reading vs. a persisted baseline, not anything computed purely
from that check's own candle window. The baseline is seeded from the
first-ever check for a symbol (which never triggers on that same call
unless the window's own move already exceeds threshold), and resets to
the current reading every time a threshold trip fires — so the "baseline"
line on each card is whichever earlier check most recently reset it, not
a fixed reference point. It's persisted in the same
`market-agent-config.json` this add-on's own threshold/prompt pushes
already write to (see "Trigger settings" above), so it survives a
Workflow Service redeploy the same way those do. Being over threshold
just means that measure is one reason **Trigger** is `true` — it does
not mean a Claude call happened; every background-loop check is a free
preview, never billed, and only the **Run AI Trend Analysis** button
actually bills one.

**The check that (re)set the current baseline is highlighted** in
Workflow History — a tinted row and a small **baseline** badge next to
its time — straight from the Workflow Service's own `NewBaselinePrice`/
`NewBaselineVolatility` fields, which are non-null only on that one
check. Only background-loop Preview checks ever (re)set the baseline; a
Trend Analysis run reads whatever baseline currently exists but never
seeds or resets it.

**Volume is reported for reference only** — it's no longer part of the
trigger at all, so its card has no threshold and no over/under status,
just the raw measured value. The upstream data source doesn't report
volume for OTC/FX-spot and precious-metals instruments in the first
place, so for XAGUSD (the default symbol) this card normally shows "—".

Each card also carries a tiny trend line across the last ~12 checks
(about an hour, at the default 5-minute cadence) — Price move and
Volatility show the threshold as a faint dashed reference so "how close"
is visible at a glance; Volume's has no reference line, matching that it
has no threshold.

Cost is estimated locally from a small rate table keyed by model name,
not fetched from Anthropic — there's no API for querying actual account
balance or cost. An unrecognized model shows "unknown" rather than a
guessed number; the table needs manual updates if pricing changes.

## History

`/share/market-agent/log.jsonl`. This is a panel convenience, not an
audit trail, so it's kept small: the most recent 50 checks, plus the
most recent 20 login-required ones tracked separately (an expired
session otherwise produces one near-identical entry every poll until
someone logs back in, which would otherwise crowd out real history out of
a single shared budget during exactly the outage you'd want history
for). Workflow detail pages only work for entries still in that window —
older links go stale and say so rather than erroring.

## Options

| Option | Default | Purpose |
|---|---|---|
| `workflow_service_host` | `192.168.0.201` | LAN address of the Workflow Service |
| `market_agent_symbol` | `XAGUSD` | Symbol this panel tracks |
| `notify_service` | *(empty)* | HA notify service name for pushes — pushes are silently skipped until this is set |
| `auth_login_url` | *(empty)* | Overrides the login link (pill + notifications) with your own WAN-reachable URL. Defaults to the Workflow Service's own login route on `workflow_service_host` (LAN-only) if left empty |
| `price_move_threshold_percent` | *(empty)* | Overrides the Workflow Service's Price move trigger threshold. Leave empty to use its own configured value |
| `volatility_threshold_percent` | *(empty)* | Overrides the Workflow Service's Volatility trigger threshold. Leave empty to use its own configured value |
| `system_prompt` | *(empty)* | Overrides the system prompt used for the Claude call. Leave empty to use the Workflow Service's own default |

## Logs

Live: the add-on's **Log** tab, or `ha addons logs market-agent`.

## Behaviour worth knowing

**Nothing secret is in this add-on.** No keys, no tokens — auth to Home
Assistant is via the Supervisor's own proxy, and the Workflow Service has
no auth on the LAN-only endpoints this add-on talks to. This repository
is public.
