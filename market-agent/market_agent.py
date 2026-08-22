#!/usr/bin/env python3
"""Background subscriber + notifier for the Market Agent panel.

Connects once to xWeb's SignalR hub (/streamHub) and stays connected,
subscribed to topic "marketagent.preview" - each broadcast is one tick of
MarketAgentBackgroundService's own loop (preview only, never a billed
Claude call - see xWeb's CLAUDE.md). No polling: signalrcore holds one
persistent connection open via with_automatic_reconnect(max_attempts=None),
so a dropped connection (xWeb pod restart, network blip) recovers on its
own. This module's own outer retry loop only exists to rebuild the
connection from scratch if the very first `start()` call itself fails
(xWeb unreachable at add-on boot) or if the transport eventually closes
for good despite that setting.

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

XWEB_HOST = os.environ.get("XWEB_HOST", "192.168.0.201")
SYMBOL = os.environ.get("MARKET_AGENT_SYMBOL", "XAGUSD")
NOTIFY_SERVICE = os.environ.get("NOTIFY_SERVICE", "").strip()
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

TOPIC = "marketagent.preview"
HUB_URL = f"http://{XWEB_HOST}/streamHub"
# WAN-reachable on purpose - kumuruku.com/saxo/login is one of the two
# paths the narrow WAN Ingress allows through (see Home/CLAUDE.md), so
# this link works even when the notification is seen away from home.
# xweb.kumuruku.com is LAN-only and would not.
SAXO_LOGIN_URL = "https://kumuruku.com/saxo/login"

SHARE = Path("/share/market-agent")
LOGFILE = SHARE / "log.jsonl"
STATEFILE = SHARE / ".notify-state.json"
MAX_ENTRIES = 500

_state_lock = threading.Lock()


def _read_state():
    try:
        return json.loads(STATEFILE.read_text())
    except Exception:
        return {"last_triggered": False, "last_saxo_auth_required": False}


def _write_state(state):
    SHARE.mkdir(parents=True, exist_ok=True)
    STATEFILE.write_text(json.dumps(state))


def notify(title, message):
    """Best-effort push via the Supervisor's Home Assistant API proxy.

    Silently does nothing if notify_service isn't configured yet, or if
    the push itself fails - a notification failure must never take down
    the subscriber thread or hide a real market/auth event from the log.
    """
    if not NOTIFY_SERVICE or not SUPERVISOR_TOKEN:
        log.info("notify skipped (not configured): %s: %s", title, message)
        return
    url = f"http://supervisor/core/api/services/notify/{NOTIFY_SERVICE}"
    payload = json.dumps({"title": title, "message": message}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log.warning("notify failed: %s", e)


def _append(entry):
    SHARE.mkdir(parents=True, exist_ok=True)
    lines = []
    if LOGFILE.exists():
        try:
            lines = LOGFILE.read_text().splitlines()
        except OSError:
            lines = []
    lines.append(json.dumps(entry))
    lines = lines[-MAX_ENTRIES:]
    LOGFILE.write_text("\n".join(lines) + "\n")


def history(limit=100):
    if not LOGFILE.exists():
        return []
    try:
        lines = LOGFILE.read_text().splitlines()
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

    with _state_lock:
        state = _read_state()

        if saxo_auth_required and not state.get("last_saxo_auth_required"):
            notify("Market Agent", f"Saxo login required: {SAXO_LOGIN_URL}")
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
            notify("Market Agent", f"Saxo login required: {SAXO_LOGIN_URL}")
            return False, f"Saxo authentication required. Log in: {SAXO_LOGIN_URL}"
        return False, f"xWeb returned {e.code}: {body}"
    except Exception as e:
        return False, f"Could not reach xWeb: {e}"


def _connect_once():
    """Build, start, and block on one hub connection until it closes for
    good. Returns when there's nothing more this connection can do -
    the caller is responsible for deciding whether/when to retry.
    """
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
    # Fires on every (re)connect, not just the first - ensures the panel
    # shows a value immediately rather than waiting up to
    # MarketAgent:PollingIntervalMinutes for the next tick, both on
    # startup and after any reconnect.
    hub.on_open(lambda: hub.send("RequestLatest", [TOPIC]))
    hub.on_close(lambda: closed.set())
    if not hub.start():
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
