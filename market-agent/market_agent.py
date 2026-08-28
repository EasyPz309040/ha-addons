#!/usr/bin/env python3
"""Background subscriber + notifier for the Market Agent panel.

Connects once to the Workflow Service's SignalR hub (/streamHub) and stays
connected, subscribed to topic "marketagent.preview" - each broadcast is
one tick of MarketAgentBackgroundService's own loop (preview only, never a
billed Claude call - see the Workflow Service's own CLAUDE.md). No
polling: signalrcore holds one persistent connection open via
with_automatic_reconnect(max_attempts=None), so a dropped connection (a
Workflow Service pod restart, network blip) recovers on its own. This
module's own outer retry loop only exists to rebuild the connection from
scratch if the very first `start()` call itself fails (Workflow Service
unreachable at add-on boot) or if the transport eventually closes for
good despite that setting.

Persists a bounded JSONL history to /share/market-agent/ - deliberately
not /share/ansible/, which is namespaced for Ansible-specific state and
has nothing to do with this feature; it just happens to share ui.py's
container.

Notifications go through the Supervisor's own proxied Home Assistant API
(config.yaml's homeassistant_api: true + the auto-injected
SUPERVISOR_TOKEN env var) - not the separate cluster-to-HA notify
plumbing documented in ACTION-PLAN.md, which exists for k3s pods that
have no Supervisor of their own. An add-on always has one, so no
long-lived token or secret is needed here.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from signalrcore.hub_connection_builder import HubConnectionBuilder

log = logging.getLogger("market_agent")

# .strip() or default, not just .get()'s default - os.environ.get() only
# falls back when the var is absent, not when it's present-but-blank. A
# blank workflow_service_host config value (e.g. from the xweb_host ->
# workflow_service_host rename not being re-entered after updating)
# would otherwise silently produce a malformed "http:///saxo/login" URL -
# real failure mode, not hypothetical, caught after a user report.
XWEB_HOST = os.environ.get("XWEB_HOST", "").strip() or "192.168.0.201"
SYMBOL = os.environ.get("MARKET_AGENT_SYMBOL", "").strip() or "XAGUSD"
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "").strip()
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Trigger thresholds + the Claude system prompt, pushed to the Workflow
# Service's own App_Data config rather than passed per-request - it's the
# autonomous background loop that needs these, and that loop is entirely
# inside the Workflow Service, never driven by a request from here. Each
# is blank by default (don't override whatever the Workflow Service
# already has configured).
PRICE_MOVE_THRESHOLD_PERCENT = os.environ.get("PRICE_MOVE_THRESHOLD_PERCENT", "").strip()
VOLATILITY_THRESHOLD_PERCENT = os.environ.get("VOLATILITY_THRESHOLD_PERCENT", "").strip()
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "").strip()
CONFIG_URL = f"http://{XWEB_HOST}/claude/MarketAgent/config"

TOPIC = "marketagent.preview"
# The Workflow Service's own auth-status topic - the literal name below
# ("saxo.authstatus") is a wire constant coming straight from its own
# topic-prefix-as-owner convention and must match exactly what it
# broadcasts; nothing in this add-on's own naming (AUTH_TOPIC, everything
# downstream of it) needs to echo that, so it doesn't. Pushed the moment
# the Workflow Service's own upstream-broker auth state actually changes
# (login, a real refresh, or once at its own startup) - not tied to
# marketagent.preview's 5-minute poll cadence at all, which is what makes
# the auth pill react immediately to a login instead of waiting for the
# next preview tick.
AUTH_TOPIC = "saxo.authstatus"
HUB_URL = f"http://{XWEB_HOST}/streamHub"
# Fallback chain, used only until the Workflow Service's own broadcast
# carries a PublicLoginUrl (see _public_login_url below - that's the
# preferred source once it's actually arriving). No domain name belongs
# in this repo's source - it's public. Defaults to XWEB_HOST (LAN-only,
# but zero configuration needed). A user who wants this link to survive
# being tapped from a notification away from home, before the Workflow
# Service side of this is live, can set auth_login_url in the add-on's
# own config to their own WAN hostname - that value lives in their
# Supervisor's stored config, never in git, so it never puts a domain in
# the repo either way.
_AUTH_LOGIN_URL_OVERRIDE = os.environ.get("AUTH_LOGIN_URL", "").strip()
_AUTH_LOGIN_URL_FALLBACK = _AUTH_LOGIN_URL_OVERRIDE or f"http://{XWEB_HOST}/saxo/login"

_public_login_url = None  # latest PublicLoginUrl seen on a broadcast, if any
_public_login_url_lock = threading.Lock()


def login_url():
    """The best currently-known login URL for the Workflow Service's
    upstream broker session.

    Prefers PublicLoginUrl straight from the Workflow Service's own
    broadcast (it derives this from its own already-configured redirect
    URI, so it's always right and needs no config here at all) - falls
    back to _AUTH_LOGIN_URL_FALLBACK only if no tick has carried one yet,
    e.g. before that field exists on the Workflow Service side, or before
    the very first tick arrives.
    """
    with _public_login_url_lock:
        return _public_login_url or _AUTH_LOGIN_URL_FALLBACK


_last_auth_status = None  # latest saxo.authstatus payload, if any have arrived yet
_auth_status_lock = threading.Lock()


def auth_status():
    """The latest saxo.authstatus push, or None if none has arrived yet
    (an older Workflow Service without this topic, or just not received
    one this run). ui.py prefers this for the auth pill when present,
    falling back to inferring it from the last marketagent.preview tick's
    Status otherwise - same defensive fields-may-be-absent pattern as
    PublicLoginUrl and the trigger threshold fields.
    """
    with _auth_status_lock:
        return _last_auth_status


SHARE = Path("/share/market-agent")
LOGFILE = SHARE / "log.jsonl"
STATEFILE = SHARE / ".notify-state.json"
# This log is a panel convenience, not an audit trail - no need to keep
# hundreds of routine ticks. The auth-required status (wire value
# "SaxoAuthRequired") is capped separately and smaller: an expired
# session produces one near-identical entry per poll until someone logs
# back in, and none of the extras beyond a handful are useful - without a
# separate cap they'd crowd out real history out of the single
# MAX_ENTRIES budget during exactly the outage you'd want history for.
# Self-healing: an existing oversized log.jsonl (from before this policy)
# gets pruned down on the very next append, no migration needed.
MAX_ENTRIES = 50
MAX_AUTH_REQUIRED_ENTRIES = 20
_LOW_VALUE_STATUSES = {"SaxoAuthRequired"}

_state_lock = threading.Lock()

# One of "connecting" (initial, before the first hub.start() attempt
# completes), "connected", "reconnecting" (signalrcore's own
# with_automatic_reconnect is mid-retry - the SAME hub object may still
# recover without this module rebuilding anything), or "disconnected"
# (this connection is being torn down; _run_forever will rebuild a fresh
# one after backoff). This is the honest thing the add-on can actually
# know - it says nothing about whether the Workflow Service's own loop is still ticking,
# only whether the pipe to it is currently up.
_connection_state = "connecting"
_connection_lock = threading.Lock()


def _set_connection_state(state):
    global _connection_state
    with _connection_lock:
        _connection_state = state


def connection_status():
    with _connection_lock:
        return _connection_state


# Watchdog state - see _watchdog_loop below. Separate lock from
# _connection_lock/_state_lock: this is written from _on_data (the hub's
# receive thread) and read from the watchdog thread, an unrelated pair to
# either of those.
_last_data_at = None
_last_data_at_lock = threading.Lock()

_current_hub = None
_current_hub_lock = threading.Lock()

# Generous multiple of the default 5-minute poll interval - long enough
# that a couple of slow/retried polls never false-trigger a reconnect,
# short enough to self-heal well within a session. Deliberately not
# market-hours-aware (no special-casing MarketClosed's own multi-hour
# gaps): a spurious reconnect during a real closed-market silence is
# cheap and harmless - it just re-requests the same cached tick - so the
# extra complexity of parsing NextRetryAfter here isn't worth it.
STALE_AFTER_SECONDS = 25 * 60


def _read_state():
    try:
        return json.loads(STATEFILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_triggered": False, "last_auth_required": False}


def _write_state(state):
    SHARE.mkdir(parents=True, exist_ok=True)
    STATEFILE.write_text(json.dumps(state), encoding="utf-8")


def notify(title, message, url=None):
    """Best-effort push via the Supervisor's Home Assistant API proxy.

    Silently does nothing if notify_service isn't configured yet, or if
    the push itself fails - a notification failure must never take down
    the subscriber thread or hide a real market/auth event from the log.

    `url`, when given, goes in the payload's data.url - the HA companion
    app field that actually makes a notification open that URL when
    tapped. Putting a URL only in `message` (as plain text, the previous
    version of this function did only that) does NOT make it tappable -
    without data.url, tapping falls back to the app's default action,
    which opens Home Assistant itself at its own configured server URL,
    not anything mentioned in the message text.
    """
    if not NOTIFY_SERVICE or not SUPERVISOR_TOKEN:
        log.info("notify skipped (not configured): %s: %s", title, message)
        return
    api_url = f"http://supervisor/core/api/services/notify/{NOTIFY_SERVICE}"
    body = {"title": title, "message": message}
    if url:
        body["data"] = {"url": url}
    payload = json.dumps(body).encode()
    req = urllib.request.Request(api_url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log.warning("notify failed: %s", e)


def _append(entry):
    SHARE.mkdir(parents=True, exist_ok=True)
    entries = history(limit=10_000)  # whatever's on disk so far, pre-trim
    entries.append(entry)
    low_value = [e for e in entries if e.get("Status") in _LOW_VALUE_STATUSES]
    normal = [e for e in entries if e.get("Status") not in _LOW_VALUE_STATUSES]
    kept = low_value[-MAX_AUTH_REQUIRED_ENTRIES:] + normal[-MAX_ENTRIES:]
    kept.sort(key=lambda e: e.get("receivedAt", 0))
    LOGFILE.write_text("\n".join(json.dumps(e) for e in kept) + "\n", encoding="utf-8")


def history(limit=100):
    if not LOGFILE.exists():
        return []
    try:
        lines = LOGFILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest():
    h = history(limit=1)
    return h[-1] if h else None


def _update_public_login_url(url):
    if not url:
        return
    global _public_login_url
    with _public_login_url_lock:
        _public_login_url = url


def _handle_auth_signal(required):
    """Login-required/resolved notification, deduped on transition.

    Single source of truth for last_auth_required so that the
    marketagent.preview topic's own auth-required status (wire value
    "SaxoAuthRequired") and the saxo.authstatus push - which can report
    the same transition independently, sometimes within moments of each
    other - can't double-notify. Self-locking: callers must NOT already
    hold _state_lock.
    """
    with _state_lock:
        state = _read_state()
        if required and not state.get("last_auth_required"):
            notify("Market Agent", f"Login required: {login_url()}", url=login_url())
        elif state.get("last_auth_required") and not required:
            notify("Market Agent", "Re-authenticated - Market Agent back online.")
            # SaxoAuthRequired ticks never touch last_triggered (see
            # _on_preview_data), so it's frozen at whatever it was right
            # before the outage started - if that was already True, the
            # first real tick after reconnecting looks like "no change"
            # to the edge-detector and gets silently suppressed, even
            # though nothing was actually being evaluated during the gap
            # and Triggered=true coming back is real, new information.
            # Confirmed for real 2026-08-28: a threshold-met notification
            # never arrived for exactly this reason. Reset here so the
            # next real tick is always treated as a fresh transition.
            state["last_triggered"] = False
        state["last_auth_required"] = required
        _write_state(state)


def _on_data(args):
    # signalrcore hands invocation args as a plain list; both topics
    # broadcast SendAsync("onData", topic, json, ct) - two arguments,
    # dispatched here by topic.
    try:
        topic, payload = args[0], args[1]
    except (IndexError, TypeError):
        return
    try:
        result = json.loads(payload)
    except ValueError:
        return
    if topic == TOPIC:
        _on_preview_data(result)
    elif topic == AUTH_TOPIC:
        _on_auth_status_data(result)


def _on_preview_data(result):
    entry = dict(result)
    entry["receivedAt"] = time.time()
    _append(entry)

    global _last_data_at
    with _last_data_at_lock:
        _last_data_at = entry["receivedAt"]

    # PascalCase throughout - MarketWorkflowResult/TriggerMetrics have no
    # [JsonProperty] overrides, so JsonConvert.SerializeObject emits keys
    # matching the C# property names exactly (Status, Metrics.Triggered,
    # Metrics.Reasons, ...). This is NOT the same casing as the HTTP
    # endpoint (/claude/MarketAgent), which goes through a different
    # serializer configured for camelCase - don't copy field names from
    # one to the other.
    status = result.get("Status")
    auth_required = status == "SaxoAuthRequired"
    metrics = result.get("Metrics") or {}
    triggered = bool(metrics.get("Triggered"))

    _update_public_login_url(result.get("PublicLoginUrl"))
    _handle_auth_signal(auth_required)

    with _state_lock:
        state = _read_state()
        # An auth-required tick carries no metrics at all - don't let a
        # stale "still triggered" state silently persist through however
        # many auth-required ticks happen before someone logs back in.
        if not auth_required:
            if triggered and not state.get("last_triggered"):
                reasons = ", ".join(metrics.get("Reasons") or [])
                notify("Market Agent",
                       f"{SYMBOL} threshold met" + (f" ({reasons})" if reasons else ""))
            state["last_triggered"] = triggered
        _write_state(state)


def _on_auth_status_data(result):
    """saxo.authstatus - see AUTH_TOPIC's own comment for why this
    exists. Also PascalCase (SaxoAuthStatus in Model.Core, same
    JsonConvert.SerializeObject as marketagent.preview).
    """
    entry = dict(result)
    entry["receivedAt"] = time.time()
    global _last_auth_status
    with _auth_status_lock:
        _last_auth_status = entry

    _update_public_login_url(result.get("PublicLoginUrl"))
    _handle_auth_signal(not result.get("Authenticated"))


# resolve_auth_login_redirect()/_NoRedirect, and the /auth-login ingress
# route that called them, were removed 2026-08-26. That route relayed
# xWeb's real redirect Location through this add-on's own ingress path -
# which works fine for anything staying inside the already-authenticated
# ingress session (a browser tab that's separately logged into this same
# HA instance), but the in-panel pill opens with target="_blank" so HA's
# companion app hands the tap off to the *system* browser/app-external
# context, which does not carry the ingress session's own auth cookie.
# That external, cookie-less request to the ingress-relative URL hit
# HA's own login flow instead of ever reaching this add-on's route -
# stuck on Home Assistant's own domain, never actually redirected to the
# real login page. login_url() below is a real, standalone URL (either
# PublicLoginUrl straight off the broadcast, or the LAN/WAN fallback) -
# exactly what notify() already uses and has always worked externally,
# with no ingress session involved at all - so the pill now links there
# directly instead of through the ingress relay.


def trigger_real_run():
    """Fire a real (billed) analysis run. Returns (ok: bool, message: str).

    A synchronous request/response from ui.py's button handler, not part
    of the background subscriber - unrelated to the loop's own ticks.
    On a 401 this also fires the relogin notification immediately, since
    a manual click getting a 401 is the clearest, most immediate signal
    that the token is actually missing right now.
    """
    url = f"http://{XWEB_HOST}/claude/MarketAgent?symbols={SYMBOL}&preview=false"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return True, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            notify("Market Agent", f"Login required: {login_url()}", url=login_url())
            return False, f"Authentication required. Log in: {login_url()}"
        return False, f"Workflow Service returned {e.code}: {body}"
    except Exception as e:
        return False, f"Could not reach the Workflow Service: {e}"


def _push_agent_config():
    """Pushes configured thresholds/prompt to the Workflow Service's
    unified config endpoint. Called from _on_open - on every (re)connect,
    not just once at add-on startup. That's what makes this self-healing:
    an xWeb redeploy wipes its own App_Data (including this config), but
    the same redeploy also drops this connection, so the very next
    reconnect re-pushes it automatically - no restart of this add-on
    required. Covers a HA config change too, since saving one restarts
    this add-on, and startup is itself a first connect.

    Fields left blank in this add-on's own config are omitted from the
    body entirely, not sent as nulls - the Workflow Service only merges
    in fields actually present, so an unconfigured field here just
    leaves whatever it already has untouched. Best-effort like notify():
    a failed push must never take down the subscriber thread.
    """
    body = {}
    if PRICE_MOVE_THRESHOLD_PERCENT:
        try:
            body["priceMoveThresholdPercent"] = float(PRICE_MOVE_THRESHOLD_PERCENT)
        except ValueError:
            log.warning("price_move_threshold_percent is not a number: %r", PRICE_MOVE_THRESHOLD_PERCENT)
    if VOLATILITY_THRESHOLD_PERCENT:
        try:
            body["volatilityThresholdPercent"] = float(VOLATILITY_THRESHOLD_PERCENT)
        except ValueError:
            log.warning("volatility_threshold_percent is not a number: %r", VOLATILITY_THRESHOLD_PERCENT)
    if SYSTEM_PROMPT:
        body["systemPrompt"] = SYSTEM_PROMPT
    if not body:
        return

    payload = json.dumps(body).encode()
    req = urllib.request.Request(CONFIG_URL, data=payload, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log.warning("push agent config failed: %s", e)


def _connect_once():
    """Build, start, and block on one hub connection until it closes for
    good. Returns when there's nothing more this connection can do -
    the caller is responsible for deciding whether/when to retry.
    """
    _set_connection_state("connecting")
    closed = threading.Event()
    hub = (HubConnectionBuilder()
           .with_url(HUB_URL, options={"verify_ssl": False})
           .with_automatic_reconnect({
               "type": "raw",
               "keep_alive_interval": 10,
               "reconnect_interval": 5,
               "max_attempts": None,  # reconnect forever once connected
           })
           .build())
    hub.on("onData", _on_data)

    def _on_open():
        _set_connection_state("connected")
        # Subscribe is what actually adds this connection to the
        # server-side SignalR group MarketAgentBackgroundService
        # broadcasts to (Groups.AddToGroupAsync in StreamHub.Subscribe) -
        # without it, Clients.Group(Topic).SendAsync(...) never reaches
        # this connection at all, no matter how long it stays open. This
        # was missing here from the start: every tick this add-on has
        # ever shown came from the one-shot RequestLatest snapshot below,
        # taken at connect/reconnect time - never a live push - which is
        # why history gaps didn't track the 5-minute poll interval at all
        # (found 2026-08-23 while investigating a reported auth-status
        # lag). Must be sent on every (re)connect, not just the
        # first, since group membership doesn't survive a reconnect
        # either.
        hub.send("Subscribe", [TOPIC])
        hub.send("Subscribe", [AUTH_TOPIC])
        # RequestLatest still matters even with a real subscription: it's
        # what makes the panel show a value immediately on connect rather
        # than waiting on the next push of either topic - marketagent.preview's
        # 5-minute poll, or saxo.authstatus's next login/refresh event.
        hub.send("RequestLatest", [TOPIC])
        hub.send("RequestLatest", [AUTH_TOPIC])
        # Self-healing config push - see _push_agent_config's own docstring
        # for why this belongs on every (re)connect, not just once at
        # startup. A plain HTTP call, not a hub method - has nothing to do
        # with SignalR itself, it's just piggybacking on "a connection was
        # just (re)established" as the trigger.
        _push_agent_config()

    def _on_close():
        _set_connection_state("disconnected")
        closed.set()

    hub.on_open(_on_open)
    hub.on_reconnect(lambda: _set_connection_state("reconnecting"))
    hub.on_close(_on_close)
    if not hub.start():
        _set_connection_state("disconnected")
        raise RuntimeError("hub.start() returned False")

    global _current_hub
    with _current_hub_lock:
        _current_hub = hub
    try:
        closed.wait()
    finally:
        with _current_hub_lock:
            if _current_hub is hub:
                _current_hub = None


def _run_forever():
    backoff = 5
    while True:
        try:
            _connect_once()
            backoff = 5  # a connection that made it up at all resets backoff
        except Exception as e:
            log.warning("market agent hub connection failed: %s", e)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


def _watchdog_loop():
    """Catches a connection that LOOKS alive but has gone deaf.

    Observed for real 2026-08-23: a client's connection survived the
    *backend* pod it was talking to being replaced (an xWeb redeploy)
    without erroring or reconnecting on its own - TCP stayed established,
    connection_status() kept saying "connected", but no further broadcast
    ever arrived. signalrcore's own keep_alive_interval didn't catch it -
    a ping that's written successfully to a half-dead socket doesn't
    prove the far end is still listening, and evidently nothing here was
    checking for a pong. Deliberately dumb: it only asks "has real data
    arrived recently enough", not "why not" - and force-closes the
    current hub if the answer is no, letting _run_forever's existing
    backoff/reconnect loop rebuild it exactly as if it had failed on its
    own. hub.stop() is a documented-safe cross-thread call (it just closes
    the underlying websocket-client socket, same effect a real network
    failure would have).
    """
    while True:
        time.sleep(60)
        _watchdog_check()


def _watchdog_check():
    if connection_status() != "connected":
        return  # already reconnecting/disconnected - _run_forever already owns this
    with _last_data_at_lock:
        last = _last_data_at
    if last is None:
        return  # nothing received on this connection yet - too early to judge
    idle = time.time() - last
    if idle <= STALE_AFTER_SECONDS:
        return
    log.warning("market agent connection looks stale (%.0fs since last tick) - forcing reconnect", idle)
    with _current_hub_lock:
        hub = _current_hub
    if hub is not None:
        try:
            hub.stop()
        except Exception as e:
            log.warning("stale-connection stop() failed: %s", e)


def start_background_thread():
    t = threading.Thread(target=_run_forever, name="market-agent-hub", daemon=True)
    t.start()
    threading.Thread(target=_watchdog_loop, name="market-agent-watchdog", daemon=True).start()
    return t
