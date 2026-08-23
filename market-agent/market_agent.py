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
# would otherwise silently produce a malformed "http:///saxo/login" -
# real failure mode, not hypothetical, caught after a user report.
XWEB_HOST = os.environ.get("XWEB_HOST", "").strip() or "192.168.0.201"
SYMBOL = os.environ.get("MARKET_AGENT_SYMBOL", "").strip() or "XAGUSD"
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "").strip()
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

TOPIC = "marketagent.preview"
HUB_URL = f"http://{XWEB_HOST}/streamHub"
# Fallback chain, used only until the Workflow Service's own broadcast
# carries a PublicLoginUrl (see _public_login_url below - that's the
# preferred source once it's actually arriving). No domain name belongs
# in this repo's source - it's public. Defaults to XWEB_HOST (LAN-only,
# but zero configuration needed). A user who wants this link to survive
# being tapped from a notification away from home, before the Workflow
# Service side of this is live, can set saxo_login_url in the add-on's
# own config to their own WAN hostname - that value lives in their
# Supervisor's stored config, never in git, so it never puts a domain in
# the repo either way.
_SAXO_LOGIN_URL_OVERRIDE = os.environ.get("SAXO_LOGIN_URL", "").strip()
_SAXO_LOGIN_URL_FALLBACK = _SAXO_LOGIN_URL_OVERRIDE or f"http://{XWEB_HOST}/saxo/login"

_public_login_url = None  # latest PublicLoginUrl seen on a broadcast, if any
_public_login_url_lock = threading.Lock()


def login_url():
    """The best currently-known Saxo login URL.

    Prefers PublicLoginUrl straight from the Workflow Service's own
    broadcast (it derives this from its own already-configured
    Saxo:RedirectUri, so it's always right and needs no config here at
    all) - falls back to _SAXO_LOGIN_URL_FALLBACK only if no tick has
    carried one yet, e.g. before that field exists on the Workflow
    Service side, or before the very first tick arrives.
    """
    with _public_login_url_lock:
        return _public_login_url or _SAXO_LOGIN_URL_FALLBACK

SHARE = Path("/share/market-agent")
LOGFILE = SHARE / "log.jsonl"
STATEFILE = SHARE / ".notify-state.json"
# This log is a panel convenience, not an audit trail - no need to keep
# hundreds of routine ticks. SaxoAuthRequired is capped separately and
# smaller: an expired session produces one near-identical entry per poll
# until someone logs back in, and none of the extras beyond a handful are
# useful - without a separate cap they'd crowd out real history out of the
# single MAX_ENTRIES budget during exactly the outage you'd want history
# for. Self-healing: an existing oversized log.jsonl (from before this
# policy) gets pruned down on the very next append, no migration needed.
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


def _read_state():
    try:
        return json.loads(STATEFILE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_triggered": False, "last_saxo_auth_required": False}


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


def _on_data(args):
    # signalrcore hands invocation args as a plain list; StreamHub
    # broadcasts SendAsync("onData", topic, json, ct) - two arguments.
    try:
        topic, payload = args[0], args[1]
    except (IndexError, TypeError):
        return
    if topic != TOPIC:
        return
    try:
        result = json.loads(payload)
    except ValueError:
        return

    entry = dict(result)
    entry["receivedAt"] = time.time()
    _append(entry)

    # PascalCase throughout - MarketWorkflowResult/TriggerMetrics have no
    # [JsonProperty] overrides, so JsonConvert.SerializeObject emits keys
    # matching the C# property names exactly (Status, Metrics.Triggered,
    # Metrics.Reasons, ...). This is NOT the same casing as the HTTP
    # endpoint (/claude/MarketAgent), which goes through a different
    # serializer configured for camelCase - don't copy field names from
    # one to the other.
    status = result.get("Status")
    saxo_auth_required = status == "SaxoAuthRequired"
    metrics = result.get("Metrics") or {}
    triggered = bool(metrics.get("Triggered"))

    public_login_url = result.get("PublicLoginUrl")
    if public_login_url:
        global _public_login_url
        with _public_login_url_lock:
            _public_login_url = public_login_url

    with _state_lock:
        state = _read_state()

        if saxo_auth_required and not state.get("last_saxo_auth_required"):
            notify("Market Agent", f"Saxo login required: {login_url()}", url=login_url())
        elif state.get("last_saxo_auth_required") and not saxo_auth_required:
            notify("Market Agent", "Saxo re-authenticated - Market Agent back to normal.")
        state["last_saxo_auth_required"] = saxo_auth_required

        # A SaxoAuthRequired tick carries no metrics at all - don't let a
        # stale "still triggered" state silently persist through however
        # many auth-required ticks happen before someone logs back in.
        if not saxo_auth_required:
            if triggered and not state.get("last_triggered"):
                reasons = ", ".join(metrics.get("Reasons") or [])
                notify("Market Agent",
                       f"{SYMBOL} threshold met" + (f" ({reasons})" if reasons else ""))
            state["last_triggered"] = triggered

        _write_state(state)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Makes urlopen surface a 3xx as an HTTPError instead of following it.

    Confirmed empirically (not just assumed) against a real 302 response:
    returning None from redirect_request causes urllib to raise HTTPError
    with the original status code and Location header intact, rather than
    silently fetching whatever the redirect points to.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_saxo_login_redirect():
    """Ask the Workflow Service where /saxo/login would send a browser,
    without going there.

    Called from ui.py's own /saxo-login route (server-side, on HAOS - a
    normal LAN device, not a sandboxed browser) so the browser itself
    never has to make a cross-origin request to XWEB_HOST at all. That
    request from a public-origin ingress page (e.g. viewing the panel via
    a public hostname) to a private-range IP is exactly what triggers
    Chrome's Local Network Access prompt - and even Allow wouldn't help,
    since the browser genuinely can't route to a LAN IP from outside the
    LAN in the first place. Relaying the real Location (Saxo's public
    authorize URL) sidesteps both problems: the browser only ever talks
    to the add-on's own origin, then goes straight to Saxo.

    Returns the Location header string, or None if the Workflow Service
    didn't respond with a redirect at all (unreachable, unexpected
    response, etc).
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"http://{XWEB_HOST}/saxo/login", timeout=10)
        return None  # a 200 here would be unexpected - GetAuthorizeUrl() always redirects
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return e.headers.get("Location")
        return None
    except Exception as e:
        log.warning("resolve_saxo_login_redirect failed: %s", e)
        return None


def trigger_real_run():
    """Fire a real (billed) analysis run. Returns (ok: bool, message: str).

    A synchronous request/response from ui.py's button handler, not part
    of the background subscriber - unrelated to the loop's own ticks.
    On a Saxo 401 this also fires the relogin notification immediately,
    since a manual click getting a 401 is the clearest, most immediate
    signal that the token is actually missing right now.
    """
    url = f"http://{XWEB_HOST}/claude/MarketAgent?symbols={SYMBOL}&preview=false"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return True, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            notify("Market Agent", f"Saxo login required: {login_url()}", url=login_url())
            return False, f"Saxo authentication required. Log in: {login_url()}"
        return False, f"Workflow Service returned {e.code}: {body}"
    except Exception as e:
        return False, f"Could not reach the Workflow Service: {e}"


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
        # Fires on every (re)connect, not just the first - ensures the
        # panel shows a value immediately rather than waiting up to
        # MarketAgent:PollingIntervalMinutes for the next tick, both on
        # startup and after any reconnect.
        hub.send("RequestLatest", [TOPIC])

    def _on_close():
        _set_connection_state("disconnected")
        closed.set()

    hub.on_open(_on_open)
    hub.on_reconnect(lambda: _set_connection_state("reconnecting"))
    hub.on_close(_on_close)
    if not hub.start():
        _set_connection_state("disconnected")
        raise RuntimeError("hub.start() returned False")
    closed.wait()


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


def start_background_thread():
    t = threading.Thread(target=_run_forever, name="market-agent-hub", daemon=True)
    t.start()
    return t
