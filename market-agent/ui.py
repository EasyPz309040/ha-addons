#!/usr/bin/env python3
"""Web UI for the Market Agent add-on, served through HA ingress.

Ingress means Home Assistant proxies this panel behind its own auth, so no
port is exposed on the LAN and there is no separate login. All paths must
be relative - HA serves the panel under a generated prefix that changes.

The actual work (the persistent xWeb subscription, history, notifications)
lives in market_agent.py - this module is just the page.
"""
import html
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import market_agent

PORT = int(os.environ.get("INGRESS_PORT", "8099"))

# connection_status() values -> (pill class, pill text). Says whether the
# pipe to xWeb is up, not whether xWeb's own loop is still ticking - that's
# the honest limit of what this add-on can actually know from its side.
CONNECTION_PILLS = {
    "connected":    ("pill-ok",      "xWeb: connected"),
    "connecting":   ("pill-unknown", "xWeb: connecting…"),
    "reconnecting": ("pill-warn",    "xWeb: reconnecting…"),
    "disconnected": ("pill-warn",    "xWeb: disconnected"),
}


def _relative_time(ts):
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 5:
        return "just now"
    if delta < 90:
        return f"{int(delta)}s ago"
    minutes = delta / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Market Agent</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; background: transparent; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
.meta {{ opacity: .7; font-size: .8rem; margin-bottom: 16px; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 10px; }}
button {{ font: inherit; padding: 7px 16px; border-radius: 7px;
  border: 1px solid rgba(127,127,127,.4); background: rgba(127,127,127,.12);
  cursor: pointer; }}
button:hover {{ background: rgba(127,127,127,.25); }}
.banner {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
  font-size: .85rem; border: 1px solid rgba(127,127,127,.35);
  background: rgba(127,127,127,.08); }}
.notice-ok {{ background: rgba(46,125,50,.14); }}
.notice-bad {{ background: rgba(198,40,40,.14); }}
h2 {{ font-size: .95rem; margin: 22px 0 8px; }}
canvas {{ width: 100%; height: 180px; display: block; }}
table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid rgba(127,127,127,.18); }}
.ok {{ color: #2e7d32; font-weight: 600; }}
.bad {{ color: #c62828; font-weight: 600; }}
.triggered {{ color: #b26a00; font-weight: 600; }}
.pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
  border-radius: 999px; font-size: .75rem; font-weight: 600; text-decoration: none; }}
.pill::before {{ content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }}
.pill-ok {{ background: rgba(46,125,50,.14); color: #2e7d32; }}
.pill-warn {{ background: rgba(198,40,40,.14); color: #c62828; }}
.pill-unknown {{ background: rgba(127,127,127,.14); color: var(--text-faint, #888); }}
a.pill:hover {{ filter: brightness(1.15); }}
</style></head><body>
<h1>Market Agent</h1>
<div class="meta">{symbol}
&middot; <span class="pill {conn_pill_cls}">{conn_pill_text}</span>
&middot; <a class="pill {saxo_pill_cls}" href="{saxo_login_url}" title="Open Saxo login">{saxo_pill_text}</a>
&middot; {last_update_text}</div>
{banner}
<div class="card">
<canvas id="chart" width="900" height="180"></canvas>
</div>
<form method="post" action="./run">
<button type="submit">Run AI Trend Analysis (billed Claude call)</button>
</form>
<h2>Recent ticks</h2>
<table><tr><th>Time</th><th>Status</th><th>Triggered</th><th>Reasons</th></tr>
{rows}
</table>
<script>
const candles = {candles_json};
const c = document.getElementById('chart');
if (candles.length > 1 && c.getContext) {{
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 900, h = 180;
  c.width = w * dpr; c.height = h * dpr;
  ctx.scale(dpr, dpr);
  const vals = candles.map(k => (k.CloseBid + k.CloseAsk) / 2);
  const min = Math.min(...vals), max = Math.max(...vals);
  const pad = 8;
  const x = i => pad + (i / (vals.length - 1)) * (w - pad * 2);
  const y = v => max === min ? h / 2 : h - pad - ((v - min) / (max - min)) * (h - pad * 2);
  ctx.strokeStyle = '#4a90d9';
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  vals.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
  ctx.stroke();
}}
</script>
</body></html>"""


def render_page(notice=None, good=True):
    # PascalCase throughout below (Status, Metrics, Triggered, Reasons,
    # EvalCandles, CloseBid/CloseAsk) - matches the C# property names on
    # MarketWorkflowResult/TriggerMetrics exactly, since JsonConvert has no
    # naming overrides for them. This is NOT the same casing the HTTP
    # endpoint uses (camelCase, a different serializer) - don't mix them up.
    entries = market_agent.history(limit=30)
    banner = ""
    if notice:
        cls = "banner " + ("notice-ok" if good else "notice-bad")
        banner = f"<div class='{cls}'>{html.escape(notice)}</div>"
    elif not entries:
        banner = "<div class='banner'>No ticks received yet - waiting on market agent workflow service's first broadcast.</div>"

    latest_status = entries[-1].get("Status") if entries else None
    if latest_status is None:
        saxo_pill_cls, saxo_pill_text = "pill-unknown", "Saxo: unknown"
    elif latest_status == "SaxoAuthRequired":
        saxo_pill_cls, saxo_pill_text = "pill-warn", "Saxo: login required"
    else:
        saxo_pill_cls, saxo_pill_text = "pill-ok", "Saxo: connected"

    conn_pill_cls, conn_pill_text = CONNECTION_PILLS.get(
        market_agent.connection_status(), ("pill-unknown", "xWeb: unknown"))
    last_update_text = "Last update: " + _relative_time(entries[-1].get("receivedAt") if entries else None)

    rows = []
    for e in reversed(entries):
        status = e.get("Status")
        metrics = e.get("Metrics") or {}
        triggered = bool(metrics.get("Triggered"))
        reasons = ", ".join(metrics.get("Reasons") or [])
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("receivedAt", 0)))
        rows.append(
            "<tr><td>{ts}</td><td class='{cls}'>{status}</td>"
            "<td class='{tcls}'>{trig}</td><td>{reasons}</td></tr>".format(
                ts=ts,
                cls="bad" if status == "SaxoAuthRequired" else "ok",
                status=html.escape(str(status or "?")),
                tcls="triggered" if triggered else "",
                trig="yes" if triggered else "no",
                reasons=html.escape(reasons)))
    if not rows:
        rows.append("<tr><td colspan='4'>No ticks yet.</td></tr>")

    candles = (entries[-1].get("EvalCandles") if entries else None) or []
    candles_json = json.dumps([
        {"CloseBid": k.get("CloseBid", 0), "CloseAsk": k.get("CloseAsk", 0)}
        for k in candles])

    return PAGE.format(
        symbol=html.escape(market_agent.SYMBOL), banner=banner,
        saxo_pill_cls=saxo_pill_cls, saxo_pill_text=html.escape(saxo_pill_text),
        saxo_login_url=html.escape(market_agent.SAXO_LOGIN_URL),
        conn_pill_cls=conn_pill_cls, conn_pill_text=html.escape(conn_pill_text),
        last_update_text=html.escape(last_update_text),
        rows="".join(rows), candles_json=candles_json)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the add-on log for the subscriber, not HTTP noise

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        return self._send(render_page())

    def do_POST(self):
        ok, msg = market_agent.trigger_real_run()
        return self._send(render_page(notice=msg, good=ok))


if __name__ == "__main__":
    market_agent.start_background_thread()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
