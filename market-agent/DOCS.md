# EasyPz Market Agent

A live view of the Workflow Service's `MarketAgentBackgroundService` loop:
a price chart, the AI analysis, and the workflow history, in three sections.

## Panel layout

The panel is three cards, top to bottom:

1. **Status** — symbol, connection/Saxo pills, last update/next check, the
   price chart, and a compact side-by-side summary (**Price move** /
   **Volatility** / **Volume**) of the *last* check — the same measure
   cards described under "Workflow detail" below, just for whichever check
   just happened rather than one you clicked into. The chart's time axis
   labels are the candles' own timestamps (real price data time, from
   Saxo), not when the add-on happened to receive the check that carried
   them. When the market's closed, Saxo has nothing newer to hand back,
   so the chart keeps showing the last real session's candles rather than
   going blank — the caption below the chart adds the date (not just the
   time) and a bold "market is likely closed" note whenever those candles
   aren't from today, so stale data is never mistaken for live price
   action.
2. **AI Analysis** — the **Run AI Analysis** button (a real, billed Claude
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

## Saxo login status

A colored pill next to the symbol shows whether the Workflow Service
currently has a valid Saxo session — green "Saxo: connected", red "Saxo:
login required", or grey "Saxo: unknown" before the first check arrives.

This is pushed from its own dedicated `saxo.authstatus` SignalR topic,
separate from `marketagent.preview` — it fires the moment a login or
token refresh actually happens on the Workflow Service side, not on the
preview loop's 5-minute cadence. That's what makes the pill (and the
login-required/resolved push notification) react within moments of
logging in rather than waiting for the next scheduled check. Older
Workflow Service versions without this topic fall back to inferring the
pill from the latest preview check's own status — laggier, but still
correct.

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

When a check's threshold condition flips from not-met to met, a push
notification fires via `notify_service` (below) — once per transition,
not repeated every check while it stays true.

Saxo login state gets the same treatment: a push fires once when a check
shows login is required, and once more when it resolves — covering both
the background loop's routine checks and the manual **Run real analysis
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

## Workflow detail

Click any row in **Workflow History** for that check's full data — every
`TriggerMetrics` field, not just the four summarized in the table, plus
that check's own candle chart. If it was a real (billed) run, the Claude
question/answer and (once the Workflow Service reports it) token counts
and an estimated cost show up here too. Nothing new is collected for
this — the full payload was already being persisted per check, this just
exposes it.

**Three measure cards up top (Price move / Volatility / Volume), side by
side** — each shows the measured value, its configured threshold, and
whether it's currently **over threshold** or **under threshold** (straight
from the Workflow Service's own `Reasons`, not re-derived here — the same
consistent pair for all three measures, including Volume, whose "threshold"
is really the baseline times a configured multiplier). Being over threshold
only means this measure is one reason **Delta threshold met** is "yes" for
this check — it does not mean a Claude call happened. Every background-loop
check is a free preview, never billed (its "Status" is never `Completed` on its
own); Delta threshold met: yes just says a real analysis *would be worth
running*. Only the **Run AI Analysis** button actually bills one. Below
the cards, a fuller breakdown for Price move and Volume: Price move and
Volatility are each computed *within that check's own lookback window*
(first candle vs. last, and the window's own high–low range) against a
fixed threshold; only Volume actually compares against the stored
baseline. So Price move gets a window-start-vs-end table, Volume gets a
real baseline-vs-current table, and the baseline snapshot
(price/volatility at the time it was last set) is shown separately as
reference only — showing all three as if they were the same kind of
comparison would misrepresent how the delta threshold is actually
evaluated. A blank baseline means either this is genuinely the first
check ever, or the Workflow Service
restarted since the last one — its state has no persistent volume, so a
redeploy resets it.

On the very first check for a symbol there's no baseline yet, so none of
the three comparisons run at all that check — the cards say "not
evaluated", not a false "under threshold". Older log entries recorded
before the Workflow Service started reporting its thresholds show
"threshold not reported yet" instead of a number — the measured value is
still shown either way.

Cost is estimated locally from a small rate table keyed by model name,
not fetched from Anthropic — there's no API for querying actual account
balance or cost. An unrecognized model shows "unknown" rather than a
guessed number; the table needs manual updates if pricing changes.

## History

`/share/market-agent/log.jsonl`. This is a panel convenience, not an
audit trail, so it's kept small: the most recent 50 checks, plus the
most recent 20 `SaxoAuthRequired` ones tracked separately (an expired
Saxo session otherwise produces one near-identical entry every poll until
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
| `saxo_login_url` | *(empty)* | Overrides the Saxo login link (pill + notifications) with your own WAN-reachable URL. Defaults to `http://<workflow_service_host>/saxo/login` (LAN-only) if left empty |

## Logs

Live: the add-on's **Log** tab, or `ha addons logs market-agent`.

## Behaviour worth knowing

**Nothing secret is in this add-on.** No keys, no tokens — auth to Home
Assistant is via the Supervisor's own proxy, and the Workflow Service has
no auth on the LAN-only endpoints this add-on talks to. This repository
is public.
