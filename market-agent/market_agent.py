#!/usr/bin/env python3
"""Background subscriber + notifier for the Market Agent panel.

Connects once to the Workflow Service's SignalR hub (/streamHub) and stays
connected, subscribed to topic "marketagent.result" - each broadcast is
one stored result row (a preview tick of MarketAgentBackgroundService's own
loop, a trigger baseline, or a billed Claude analysis - see the Workflow
Service's own CLAUDE.md). History lives in the Workflow Service's database:
it is loaded over GET /market-agent/GetResults, then live rows are appended
by id, and after every (re)connect GetResults?afterId= catches up on what
was missed (the topic has no replay). No
polling: signalrcore holds one persistent connection open via
with_automatic_reconnect(max_attempts=None), so a dropped connection (a
Workflow Service pod restart, network blip) recovers on its own. This
module's own outer retry loop only exists to rebuild the connection from
scratch if the very first `start()` call itself fails (Workflow Service
unreachable at add-on boot) or if the transport eventually closes for
good despite that setting.

Only the notification dedupe state is persisted locally, under
/share/market-agent/ - deliberately not /share/ansible/, which is
namespaced for Ansible-specific state and has nothing to do with this
feature; it just happens to share ui.py's container.

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
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from signalrcore.hub_connection_builder import HubConnectionBuilder

from models import MarketAgentResultRow, SaxoAuthStatus

log = logging.getLogger("market_agent")

# .strip() or default, not just .get()'s default - os.environ.get() only
# falls back when the var is absent, not when it's present-but-blank. A
# blank workflow_service_host config value (e.g. from the xweb_host ->
# workflow_service_host rename not being re-entered after updating)
# would otherwise silently produce a malformed "https:///saxo/login" URL -
# real failure mode, not hypothetical, caught after a user report.
#
# xweb.kumuruku.com, not the old 192.168.0.201 - the Workflow Service's
# xweb-lan Service stopped being directly LAN-reachable on 2026-08-29 in
# favor of a real, LAN-only, HTTPS Traefik Ingress (lan-only Middleware +
# a genuine Let's Encrypt certificate, same as pihole/cows/rancher). This
# add-on's own workflow_service_host config still defaults to whichever
# hostname is actually reachable, so nothing but this one default value
# needed to change here.
XWEB_HOST = os.environ.get("XWEB_HOST", "").strip() or "xweb.kumuruku.com"
SYMBOL = os.environ.get("MARKET_AGENT_SYMBOL", "").strip() or "XAGUSD"
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "").strip()
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _parse_hhmm(value):
    """Minutes since midnight for "HH:MM", or None if blank/absent/invalid
    (bashio::config prints the literal "null" for an unset option)."""
    try:
        hours, minutes = value.strip().split(":")
        hours, minutes = int(hours), int(minutes)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


# Quiet hours for mobile-app notifications, in the container's local time
# (the Supervisor passes Home Assistant's configured timezone as TZ).
# Either bound blank disables silencing. A window whose start is later
# than its end (the 22:30-07:00 default) wraps past midnight.
SILENCE_START = _parse_hhmm(os.environ.get("SILENCE_START", ""))
SILENCE_END = _parse_hhmm(os.environ.get("SILENCE_END", ""))


def in_silence_window(now=None):
    if SILENCE_START is None or SILENCE_END is None or SILENCE_START == SILENCE_END:
        return False
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    if SILENCE_START < SILENCE_END:
        return SILENCE_START <= minute < SILENCE_END
    return minute >= SILENCE_START or minute < SILENCE_END

# Trigger thresholds + the Claude system prompt, pushed to the Workflow
# Service's own App_Data config rather than passed per-request - it's the
# autonomous background loop that needs these, and that loop is entirely
# inside the Workflow Service, never driven by a request from here. Each
# is blank by default (don't override whatever the Workflow Service
# already has configured).
PRICE_MOVE_THRESHOLD_PERCENT = os.environ.get("PRICE_MOVE_THRESHOLD_PERCENT", "").strip()
VOLATILITY_THRESHOLD_PERCENT = os.environ.get("VOLATILITY_THRESHOLD_PERCENT", "").strip()
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "").strip()
CONFIG_URL = f"https://{XWEB_HOST}/config/Maintain"
ANALYZE_URL = f"https://{XWEB_HOST}/claude/MarketAgentAnalyze"
RESULTS_URL = f"https://{XWEB_HOST}/market-agent/GetResults"

TOPIC = "marketagent.result"
# The Workflow Service's own auth-status topic - the literal name below
# ("saxo.authstatus") is a wire constant coming straight from its own
# topic-prefix-as-owner convention and must match exactly what it
# broadcasts; nothing in this add-on's own naming (AUTH_TOPIC, everything
# downstream of it) needs to echo that, so it doesn't. Pushed the moment
# the Workflow Service's own upstream-broker auth state actually changes
# (login, a real refresh, or once at its own startup) - not tied to
# marketagent.result's 5-minute poll cadence at all, which is what makes
# the auth pill react immediately to a login instead of waiting for the
# next preview tick.
AUTH_TOPIC = "saxo.authstatus"
HUB_URL = f"https://{XWEB_HOST}/streamHub"
# Fallback chain, used only until the Workflow Service's own broadcast
# carries a PublicLoginUrl (see _public_login_url below - that's the
# preferred source once it's actually arriving). No domain name belongs
# in this repo's source - it's public. Defaults to XWEB_HOST (LAN-only
# via the lan-only Middleware on xweb.kumuruku.com's own Ingress - tapping
# this link from off the LAN gets a 403 there, same practical limitation
# as before when it was just an unreachable LAN IP, not a new one). A
# user who wants this link to survive being tapped from a notification
# away from home, before the Workflow Service side of this is live, can
# set auth_login_url in the add-on's own config to their own WAN hostname
# - that value lives in their Supervisor's stored config, never in git,
# so it never puts a domain in the repo either way.
_AUTH_LOGIN_URL_OVERRIDE = os.environ.get("AUTH_LOGIN_URL", "").strip()
_AUTH_LOGIN_URL_FALLBACK = _AUTH_LOGIN_URL_OVERRIDE or f"https://{XWEB_HOST}/saxo/login"

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
    falling back to inferring it from the last result row's
    Status otherwise - same defensive fields-may-be-absent pattern as
    PublicLoginUrl and the trigger threshold fields.
    """
    with _auth_status_lock:
        return _last_auth_status


SHARE = Path("/share/market-agent")
STATEFILE = SHARE / ".notify-state.json"
# The in-memory view of the Workflow Service's stored results is a panel
# convenience, not an audit trail - the database is the record. The
# auth-required status (wire value "SaxoAuthRequired") is capped
# separately and smaller: an expired session produces one near-identical
# entry per poll until someone logs back in, and none of the extras beyond
# a handful are useful - without a separate cap they'd crowd out real
# history out of the MAX_ENTRIES budget during exactly the outage you'd
# want history for. Completed (billed) analyses are capped separately too,
# so a run of previews never pushes the last real analysis out of view.
MAX_ENTRIES = 50
MAX_AUTH_REQUIRED_ENTRIES = 20
MAX_COMPLETED_ENTRIES = 30
_LOW_VALUE_STATUSES = {"SaxoAuthRequired"}

# Guards _entries/_cursor and serializes the notification edge-detection
# that follows each row, so a live row and a REST catch-up can't interleave.
_ingest_lock = threading.RLock()
_entries = []  # panel-shaped entries, oldest first (see _entry_from_row)
_cursor = None  # highest row id ingested so far; GetResults?afterId= resumes from it

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


# Watchdog state - see _watchdog_loop below.
_current_hub = None
_current_closed = None  # the Event _connect_once blocks on for _current_hub
_current_hub_lock = threading.Lock()


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

    Silently does nothing if notify_service isn't configured yet, inside
    the silence_start-silence_end window (dropped, not queued), or if
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
    if in_silence_window():
        log.info("notify skipped (silence window): %s: %s", title, message)
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


def _pascal(value):
    """Re-key a REST/topic (camelCase) payload to the PascalCase ui.py
    reads, recursively. One adapter at the boundary instead of casing
    logic spread through the renderer; values are untouched."""
    if isinstance(value, dict):
        return {(k[:1].upper() + k[1:]): _pascal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_pascal(v) for v in value]
    return value


def _entry_from_row(row, result):
    """Panel entry for any row carrying a result (Preview, Completed,
    MarketClosed, or a kind added later - Kind stays a plain string here so
    an unknown one is tolerated, not rejected). receivedAt is the row's own
    RunAt, so an entry keeps the same identity (and ui.py's tick?ts= link
    keeps working) across add-on restarts."""
    entry = _pascal(result)
    entry["RowId"] = row.id
    entry["receivedAt"] = row.run_at.timestamp()
    return entry


def _trim(entries):
    low_value = [e for e in entries if e.get("Status") in _LOW_VALUE_STATUSES]
    completed = [e for e in entries if e.get("Status") == "Completed"]
    normal = [e for e in entries
              if e.get("Status") not in _LOW_VALUE_STATUSES and e.get("Status") != "Completed"]
    kept = (low_value[-MAX_AUTH_REQUIRED_ENTRIES:] + completed[-MAX_COMPLETED_ENTRIES:]
            + normal[-MAX_ENTRIES:])
    kept.sort(key=lambda e: e.get("receivedAt", 0))
    return kept


def history(limit=100):
    with _ingest_lock:
        return list(_entries[-limit:])


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
    result rows' own auth-required status (wire value "SaxoAuthRequired")
    and the saxo.authstatus push - which can report the same transition
    independently, sometimes within moments of each other - can't
    double-notify. Self-locking: callers must NOT already
    hold _state_lock.
    """
    with _state_lock:
        state = _read_state()
        if required and not state.get("last_auth_required"):
            notify("Market Agent", f"Login required: {login_url()}", url=login_url())
        elif state.get("last_auth_required") and not required:
            notify("Market Agent", "Re-authenticated - Market Agent back online.")
            # SaxoAuthRequired ticks never touch last_triggered (see
            # _on_result_row), so it's frozen at whatever it was right
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
    # signalrcore hands invocation args as a plain list; every topic
    # broadcasts SendAsync("onData", topic, json, ct) - two arguments,
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
        _on_result_row(result)
    elif topic == AUTH_TOPIC:
        _on_auth_status_data(result)


def describe_reasons(reasons, current_price, baseline_price):
    """Reasons entries are the Workflow Service's own wire values
    ("PriceMove", "Volatility") - just the name of whichever threshold
    tripped, no direction. Direction has to come from CurrentPrice vs
    BaselinePrice, NOT from PriceMovePercent's sign - confirmed with the
    xWeb session that PriceMovePercent is Math.Abs(...) server-side, an
    unsigned magnitude. An earlier version of this function used
    PriceMovePercent > 0 as the direction test, which is a real bug: an
    unsigned value is never negative, so it always said "Upward" -
    caught 2026-09-23 when a confirmed-downward tick (verified against
    the server's own Evaluate() math) still showed "Upward price move"
    in the UI and a notification. Both ui.py's table/detail rendering
    and this module's own notify() call through here, so the two can't
    say something different for the same tick. Every reason other than
    PriceMove passes through unchanged.
    """
    if not reasons:
        return ""
    have_direction = current_price is not None and baseline_price is not None
    described = []
    for r in reasons:
        if r == "PriceMove" and have_direction:
            described.append("Upward price move" if current_price > baseline_price else "Downward price move")
        else:
            described.append(r)
    return ", ".join(described)


_seen_ids = set()
_history_loaded = False


def _on_result_row(raw, notify_events=True):
    """One marketagent.result message / GetResults row. Idempotent on the
    row's id, so a live row that also arrives in a REST catch-up is only
    handled once.

    Validated against models.MarketAgentResultRow (mirrored from the
    Workflow Service's contract, see models.py's docstring for why it's
    hand-written rather than generated) before anything downstream trusts a
    single field off it - a rename/type change on xWeb's side now fails
    loudly here instead of silently producing None everywhere it's read.
    The stored entry is the raw result dict re-keyed to PascalCase, so
    unvalidated fields survive - validating doesn't mean narrowing what's
    kept.

    notify_events=False is for rows loaded as history: they populate the
    panel only, and never drive the login/threshold notifications.
    """
    global _cursor
    try:
        row = MarketAgentResultRow.model_validate(raw)
    except ValidationError as e:
        log.error("marketagent.result row failed schema validation: %s", e)
        return

    with _ingest_lock:
        if row.id in _seen_ids:
            return
        _seen_ids.add(row.id)
        _cursor = row.id if _cursor is None else max(_cursor, row.id)
        # Baseline rows only exist so xWeb survives restarts; the panel reads
        # the baseline from each result's own Metrics.
        if row.kind == "Baseline" or row.result is None:
            return

        entry = _entry_from_row(row, raw["result"])
        _entries.append(entry)
        _entries[:] = _trim(_entries)

        if not notify_events:
            return
        parsed = row.result
        auth_required = parsed.status == "SaxoAuthRequired"
        metrics = parsed.metrics
        triggered = bool(metrics and metrics.triggered)

        _update_public_login_url(parsed.public_login_url)
        _handle_auth_signal(auth_required)

        with _state_lock:
            state = _read_state()
            # An auth-required tick carries no metrics at all - don't let a
            # stale "still triggered" state silently persist through however
            # many auth-required ticks happen before someone logs back in.
            if not auth_required:
                if triggered and not state.get("last_triggered"):
                    reasons = describe_reasons(metrics.reasons if metrics else None,
                                                metrics.current_price if metrics else None,
                                                metrics.baseline_price if metrics else None)
                    notify("Market Agent",
                           f"{SYMBOL} threshold met" + (f" ({reasons})" if reasons else ""))
                state["last_triggered"] = triggered
            _write_state(state)


def _fetch_rows(**params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    with urllib.request.urlopen(f"{RESULTS_URL}?{query}", timeout=15) as resp:
        return json.loads(resp.read().decode())


def _sync_history():
    """Brings the panel's entries up to date from GetResults.

    First success: loads recent Preview, Completed and MarketClosed rows
    (the latter carries NextRetryAfter for the panel's reopen line) as history
    (no notifications) and runs only the newest Preview row through the
    notification logic, so the login/threshold state matches the live
    system without replaying old events. Afterwards: everything newer than
    the cursor, in order, as live rows - this is the post-reconnect
    catch-up, since marketagent.result has no RequestLatest replay. Held
    under _ingest_lock so a live row can't be ingested between the fetch
    and the loop. Best-effort: a failure leaves _history_loaded False and
    the watchdog loop retries. Returns how many rows it newly ingested.
    """
    global _history_loaded
    with _ingest_lock:
        before = len(_seen_ids)
        try:
            if _history_loaded:
                for raw in _fetch_rows(afterId=_cursor, limit=600):
                    _on_result_row(raw)
                return len(_seen_ids) - before
            rows = (_fetch_rows(kind="Preview", limit=MAX_ENTRIES + MAX_AUTH_REQUIRED_ENTRIES)
                    + _fetch_rows(kind="Completed", limit=MAX_COMPLETED_ENTRIES)
                    + _fetch_rows(kind="MarketClosed", limit=10))
        except Exception as e:
            log.warning("market agent history sync failed: %s", e)
            return 0
        rows.sort(key=lambda r: r.get("id", 0))
        fresh = _cursor is None
        last_preview = max((r["id"] for r in rows if r.get("kind") == "Preview"), default=None)
        for raw in rows:
            _on_result_row(raw, notify_events=fresh and raw.get("id") == last_preview)
        _history_loaded = True
        return len(_seen_ids) - before


def _on_auth_status_data(result):
    """saxo.authstatus - see AUTH_TOPIC's own comment for why this
    exists. Validated against models.SaxoAuthStatus, which is
    hand-maintained (not derived from an OpenAPI schema - xWeb has no
    REST endpoint for this class, see that model's own docstring).
    """
    entry = dict(result)
    entry["receivedAt"] = time.time()
    global _last_auth_status
    with _auth_status_lock:
        _last_auth_status = entry

    try:
        parsed = SaxoAuthStatus.model_validate(result)
    except ValidationError as e:
        log.error("saxo.authstatus payload failed schema validation: %s", e)
        return

    _update_public_login_url(parsed.public_login_url)
    _handle_auth_signal(not parsed.authenticated)


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
    # No question: xWeb falls back to its configured MarketAgent:DefaultQuestion.
    req = urllib.request.Request(
        ANALYZE_URL, data=json.dumps({"symbols": [SYMBOL]}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
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

    payload = json.dumps({"name": "MarketAgent", "marketAgent": body}).encode()
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
           # No verify_ssl override any more - HUB_URL is https:// against a
           # real Let's Encrypt certificate now (xweb.kumuruku.com's Traefik
           # Ingress, since 2026-08-29), not a bare LAN IP that would have
           # needed one.
           .with_url(HUB_URL)
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
        # server-side SignalR group the results are broadcast to
        # (Groups.AddToGroupAsync in StreamHub.Subscribe) - without it,
        # Clients.Group(Topic).SendAsync(...) never reaches this
        # connection at all, no matter how long it stays open. Must be
        # sent on every (re)connect, not just the first, since group
        # membership doesn't survive a reconnect either.
        hub.send("Subscribe", [TOPIC])
        hub.send("Subscribe", [AUTH_TOPIC])
        # marketagent.result has no RequestLatest replay: subscribe first,
        # then load/catch up over REST (de-duplicated on row id), so no row
        # can fall in the gap between the two.
        _sync_history()
        # saxo.authstatus does replay: RequestLatest is what makes the auth
        # pill show a value immediately on connect rather than waiting on
        # the next login/refresh event.
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

    global _current_hub, _current_closed
    with _current_hub_lock:
        _current_hub = hub
        _current_closed = closed
    try:
        closed.wait()
    finally:
        with _current_hub_lock:
            if _current_hub is hub:
                _current_hub = None
                _current_closed = None


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
    checking for a pong.

    Silence can't be the signal any more: results are only stored while
    the market is open and Saxo is authenticated, so a quiet topic is
    normal for hours. Instead every pass polls GetResults?afterId= - cheap,
    and also the retry for a failed connect-time load. A row that shows up
    over REST but never arrived on the hub proves the hub connection is
    deaf, so it is force-closed, letting _run_forever's existing
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
    if not _history_loaded:
        _sync_history()  # the connect-time load failed (xWeb up, results endpoint not yet)
        return
    if _sync_history() == 0:
        return
    log.warning("market agent got new rows over REST that the hub never delivered - forcing reconnect")
    with _current_hub_lock:
        hub = _current_hub
        closed = _current_closed
    if hub is not None:
        try:
            hub.stop()
        except Exception as e:
            log.warning("stale-connection stop() failed: %s", e)
    # hub.stop() alone is not enough: signalrcore 1.0.2 latches
    # manually_closing on the first call (every later stop() returns
    # immediately), and on a half-dead socket the close callback that would
    # fire _on_close never comes - so _connect_once stayed blocked on
    # closed.wait(), state stayed "connected", and this check re-fired every
    # pass to no effect (found 2026-09-30: no tick for 5 days, and a
    # stale saxo.authstatus kept the pill on "login required" through a
    # successful login). Releasing _connect_once directly lets _run_forever
    # abandon the dead hub and build a fresh one.
    if closed is not None:
        _set_connection_state("disconnected")
        closed.set()


def start_background_thread():
    # The pre-database local history; the Workflow Service owns it now.
    try:
        (SHARE / "log.jsonl").unlink(missing_ok=True)
    except OSError:
        pass
    t = threading.Thread(target=_run_forever, name="market-agent-hub", daemon=True)
    t.start()
    threading.Thread(target=_watchdog_loop, name="market-agent-watchdog", daemon=True).start()
    return t
