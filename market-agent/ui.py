#!/usr/bin/env python3
"""Web UI for the Market Agent add-on, served through HA ingress.

Ingress means Home Assistant proxies this panel behind its own auth, so no
port is exposed on the LAN and there is no separate login. All paths must
be relative - HA serves the panel under a generated prefix that changes.

The actual work (the persistent Workflow Service subscription, history,
notifications) lives in market_agent.py - this module is just the page.
"""
import html
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import market_agent

PORT = int(os.environ.get("INGRESS_PORT", "8099"))

# connection_status() values -> (pill class, pill text). Says whether the
# pipe to the Workflow Service is up, not whether its own loop is still
# ticking - that's the honest limit of what this add-on can actually know
# from its side. "Workflow Service" (not xWeb) deliberately - that's an
# internal solution name, not something a user of this add-on needs to
# know or see.
CONNECTION_PILLS = {
    "connected":    ("pill-ok",      "Workflow Service: connected"),
    "connecting":   ("pill-unknown", "Workflow Service: connecting…"),
    "reconnecting": ("pill-warn",    "Workflow Service: reconnecting…"),
    "disconnected": ("pill-warn",    "Workflow Service: disconnected"),
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


def _format_span(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h {minutes % 60}m"


def _parse_dotnet_dt(s):
    """Parse a Newtonsoft-serialized UTC DateTime (RunAt, NextRetryAfter).

    Always has a trailing Z (RunAt/NextRetryAfter are always DateTime.UtcNow
    or derived from it on the Workflow Service side) and up to 7 fractional-second
    digits (.NET ticks), one more than datetime.fromisoformat tolerates -
    truncate to microseconds rather than assume a fixed precision.
    """
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        if "." in s:
            head, frac = s.split(".", 1)
            s = f"{head}.{frac[:6]}"
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _fmt_pct(v):
    return f"{v:.2f}%" if isinstance(v, (int, float)) else "—"


def _fmt_num(v):
    return f"{v:,.0f}" if isinstance(v, (int, float)) else "—"


def _fmt_candle_time(s):
    dt = _parse_dotnet_dt(s)
    return dt.strftime("%H:%M") if dt else ""


def _fmt_dt(s):
    dt = _parse_dotnet_dt(s)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _candles_payload(entry):
    """(candles list, candles_json, baseline_price_json) for one tick -
    shared by the main page (latest tick) and the per-tick detail page
    (whichever tick you clicked through to), so the chart-drawing JS only
    needs to exist once.
    """
    candles = entry.get("EvalCandles") or []
    candles_json = json.dumps([
        {"CloseBid": k.get("CloseBid", 0), "CloseAsk": k.get("CloseAsk", 0),
         "Time": _fmt_candle_time(k.get("Time"))}
        for k in candles])
    baseline_price = (entry.get("Metrics") or {}).get("BaselinePrice")
    baseline_price_json = json.dumps(baseline_price) if isinstance(baseline_price, (int, float)) else "null"
    return candles, candles_json, baseline_price_json


# Shared by both PAGE and TICK_PAGE - plain JS, not run through .format()
# itself, so braces here are normal (single), not doubled like the rest
# of this file's templates. Takes a canvas element id plus the same
# candles/baseline data _candles_payload() produces, draws axis labels,
# a current-price marker, and (if given) a dashed baseline reference line.
CHART_JS_FN = """
function drawMarketChart(canvasId, candles, baselinePrice) {
  const c = document.getElementById(canvasId);
  if (!c || candles.length < 2 || !c.getContext) return;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = c.clientWidth || 900, cssH = 220;
  c.width = cssW * dpr; c.height = cssH * dpr;
  ctx.scale(dpr, dpr);
  ctx.font = '11px system-ui, sans-serif';

  const vals = candles.map(k => (k.CloseBid + k.CloseAsk) / 2);
  const padL = 62, padR = 10, padT = 10, padB = 20;
  let min = Math.min(...vals), max = Math.max(...vals);
  if (baselinePrice !== null) { min = Math.min(min, baselinePrice); max = Math.max(max, baselinePrice); }
  if (max === min) { max += 1; min -= 1; }

  const x = i => padL + (i / (vals.length - 1)) * (cssW - padL - padR);
  const y = v => cssH - padB - ((v - min) / (max - min)) * (cssH - padT - padB);

  const muted = getComputedStyle(document.body).color;
  ctx.strokeStyle = muted; ctx.globalAlpha = .25; ctx.lineWidth = 1;
  [min, max].forEach(v => {
    ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(cssW - padR, y(v)); ctx.stroke();
  });
  ctx.globalAlpha = 1;

  ctx.fillStyle = muted; ctx.textBaseline = 'middle';
  ctx.textAlign = 'right';
  ctx.fillText(max.toFixed(4), padL - 8, y(max));
  ctx.fillText(min.toFixed(4), padL - 8, y(min));

  if (baselinePrice !== null) {
    ctx.save();
    ctx.strokeStyle = '#b26a00'; ctx.globalAlpha = .6; ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(padL, y(baselinePrice)); ctx.lineTo(cssW - padR, y(baselinePrice)); ctx.stroke();
    ctx.restore();
    ctx.fillStyle = '#b26a00'; ctx.textAlign = 'left'; ctx.globalAlpha = 1;
    ctx.fillText('baseline ' + baselinePrice.toFixed(4), padL + 4, y(baselinePrice) - 6);
  }

  ctx.strokeStyle = '#4a90d9';
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  vals.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
  ctx.stroke();

  const lastX = x(vals.length - 1), lastY = y(vals[vals.length - 1]);
  ctx.fillStyle = '#4a90d9';
  ctx.beginPath(); ctx.arc(lastX, lastY, 3, 0, 7); ctx.fill();
  ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
  ctx.fillText(vals[vals.length - 1].toFixed(4), lastX - 6, lastY - 6);

  ctx.fillStyle = muted; ctx.globalAlpha = .7; ctx.textBaseline = 'alphabetic';
  const times = candles.map(k => k.Time || '');
  if (times[0]) { ctx.textAlign = 'left'; ctx.fillText(times[0], padL, cssH - 4); }
  if (times[times.length - 1]) { ctx.textAlign = 'right'; ctx.fillText(times[times.length - 1], cssW - padR, cssH - 4); }
}
"""


def _next_check_text(entries):
    """What the loop is expected to do next, and how confidently we know it.

    The Workflow Service's loop has two modes: a flat poll interval
    normally, or - when the market's closed - sleeping until an exact
    reopen time it tells us via NextRetryAfter. We only ever get the
    *interval itself* (not exposed in the payload) by observing the gap
    between two consecutive non-closed ticks, so that case is explicitly
    labelled "estimated"; the market-closed case is exact, straight from
    the Workflow Service.
    """
    if not entries:
        return ""
    latest = entries[-1]
    now = datetime.now(timezone.utc)

    if latest.get("Status") == "MarketClosed":
        reopen = _parse_dotnet_dt(latest.get("NextRetryAfter"))
        if reopen:
            remaining = (reopen - now).total_seconds()
            if remaining <= 0:
                return "Market closed — reopening any moment"
            return f"Market closed — reopens in {_format_span(remaining)}"
        return "Market closed"

    if len(entries) >= 2:
        last_run = _parse_dotnet_dt(latest.get("RunAt"))
        prev_run = _parse_dotnet_dt(entries[-2].get("RunAt"))
        if last_run and prev_run and entries[-2].get("Status") != "MarketClosed":
            interval = (last_run - prev_run).total_seconds()
            if interval > 0:
                remaining = interval - (now - last_run).total_seconds()
                if remaining <= 0:
                    return "Next check: any moment (estimated)"
                return f"Next check: ~{_format_span(remaining)} (estimated)"

    return "Next check: unknown yet"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Market Agent</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; background: transparent; }}
h1 {{ font-size: 1.6rem; margin: 0 0 4px; }}
.meta {{ display: flex; align-items: center; flex-wrap: wrap; gap: .5rem 1rem;
  font-size: .8rem; opacity: .7; margin-bottom: 16px; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 10px; }}
button {{ font: inherit; font-weight: 700; font-size: .85rem; padding: 10px 22px;
  border-radius: 999px; border: none; background: #039be5; color: #fff;
  cursor: pointer; }}
button:hover {{ background: #0288d1; }}
.hint {{ font-size: .78rem; opacity: .65; margin: 6px 0 0; }}
.banner {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
  font-size: .85rem; border: 1px solid rgba(127,127,127,.35);
  background: rgba(127,127,127,.08); }}
.notice-ok {{ background: rgba(46,125,50,.14); }}
.notice-bad {{ background: rgba(198,40,40,.14); }}
h2 {{ font-size: .95rem; margin: 22px 0 8px; }}
canvas {{ width: 100%; height: 220px; display: block; }}
.chart-caption {{ font-size: .78rem; opacity: .65; margin: -4px 0 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid rgba(127,127,127,.18); }}
td.num {{ font-variant-numeric: tabular-nums; text-align: right; }}
th.num {{ text-align: right; }}
.ok {{ color: #2e7d32; font-weight: 600; }}
.bad {{ color: #c62828; font-weight: 600; }}
.triggered {{ color: #b26a00; font-weight: 600; }}
.pill {{ display: inline-flex; align-items: center; gap: 6px;
  font: inherit; color: inherit; text-decoration: none; }}
.pill::before {{ content: ""; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.pill-ok::before {{ background: #43a047; }}
.pill-warn::before {{ background: #e53935; }}
.pill-unknown::before {{ background: #9e9e9e; }}
a.pill:hover {{ text-decoration: underline; }}
</style></head><body>
<h1>Market Agent</h1>
<div class="meta">
<span>{symbol}</span>
<span class="pill {conn_pill_cls}">{conn_pill_text}</span>
<a class="pill {saxo_pill_cls}" href="{saxo_login_url}" target="_blank" rel="noopener noreferrer" title="Open Saxo login">{saxo_pill_text}</a>
<span>{last_update_text}</span>
<span>{next_check_text}</span>
</div>
{banner}
<div class="card">
<canvas id="chart" width="900" height="220"></canvas>
</div>
<p class="chart-caption">{chart_caption}</p>
<form method="post" action="./run">
<button type="submit">Run AI Analysis</button>
</form>
<p class="hint">Billed Claude call</p>
<h2>Recent ticks</h2>
<table><tr><th>Time</th><th>Status</th><th>Triggered</th><th>Reasons</th>
<th class="num">Price move</th><th class="num">Volatility</th><th class="num">Volume</th></tr>
{rows}
</table>
<script>
{chart_js_fn}
drawMarketChart('chart', {candles_json}, {baseline_price_json});
</script>
</body></html>"""


TICK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Market Agent - Tick detail</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; max-width: 700px; margin-inline: auto; }}
h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
.back {{ font-size: .85rem; opacity: .7; margin-bottom: 12px; display: inline-block; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 14px; }}
canvas {{ width: 100%; height: 220px; display: block; }}
table {{ width: 100%; border-collapse: collapse; font-size: .85rem; margin-bottom: 14px; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(127,127,127,.18); }}
td.num {{ font-variant-numeric: tabular-nums; }}
th {{ width: 40%; opacity: .7; font-weight: 600; }}
h2 {{ font-size: .95rem; margin: 20px 0 8px; }}
pre {{ white-space: pre-wrap; word-break: break-word; font-size: .8rem;
  background: rgba(127,127,127,.1); padding: 10px 12px; border-radius: 8px; }}
</style></head><body>
<a class="back" href="./">&larr; back to Market Agent</a>
<h1>Tick detail — {ts}</h1>
{chart_block}
<table>
{metric_rows}
</table>
{signal_block}
</body></html>"""


def _render_chart_block(entry, caption):
    candles, candles_json, baseline_price_json = _candles_payload(entry)
    if len(candles) < 2:
        return f"<p>{html.escape(caption)}</p>"
    return (
        "<div class=\"card\"><canvas id=\"chart\"></canvas></div>"
        f"<p style=\"font-size:.8rem;opacity:.65;margin-top:-6px\">{html.escape(caption)}</p>"
        f"<script>{CHART_JS_FN}\ndrawMarketChart('chart', {candles_json}, {baseline_price_json});</script>"
    )


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
        market_agent.connection_status(), ("pill-unknown", "Workflow Service: unknown"))
    last_update_text = "Last update: " + _relative_time(entries[-1].get("receivedAt") if entries else None)
    next_check_text = _next_check_text(entries)

    rows = []
    for e in reversed(entries):
        status = e.get("Status")
        metrics = e.get("Metrics") or {}
        triggered = bool(metrics.get("Triggered"))
        reasons = ", ".join(metrics.get("Reasons") or [])
        received_at = e.get("receivedAt", 0)
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(received_at))
        rows.append(
            "<tr><td><a href='./tick?ts={raw_ts}'>{ts}</a></td><td class='{cls}'>{status}</td>"
            "<td class='{tcls}'>{trig}</td><td>{reasons}</td>"
            "<td class='num'>{move}</td><td class='num'>{vol}</td><td class='num'>{volume}</td></tr>".format(
                raw_ts=received_at,
                ts=ts,
                cls="bad" if status == "SaxoAuthRequired" else "ok",
                status=html.escape(str(status or "?")),
                tcls="triggered" if triggered else "",
                trig="yes" if triggered else "no",
                reasons=html.escape(reasons),
                move=_fmt_pct(metrics.get("PriceMovePercent")),
                vol=_fmt_pct(metrics.get("AvgVolatilityPercent")),
                volume=_fmt_num(metrics.get("AvgVolume"))))
    if not rows:
        rows.append("<tr><td colspan='7'>No ticks yet.</td></tr>")

    latest_entry = entries[-1] if entries else {}
    candles, candles_json, baseline_price_json = _candles_payload(latest_entry)
    baseline_price = (latest_entry.get("Metrics") or {}).get("BaselinePrice")

    if len(candles) > 1:
        start, end = _fmt_candle_time(candles[0].get("Time")), _fmt_candle_time(candles[-1].get("Time"))
        chart_caption = (f"{market_agent.SYMBOL} mid price (bid/ask average) — "
                          f"last {len(candles)} candles, {start}–{end}"
                          + (" · amber dashed line is the trigger baseline" if baseline_price is not None else ""))
    else:
        chart_caption = "No candle data in the latest tick yet."

    return PAGE.format(
        symbol=html.escape(market_agent.SYMBOL), banner=banner,
        saxo_pill_cls=saxo_pill_cls, saxo_pill_text=html.escape(saxo_pill_text),
        saxo_login_url="./saxo-login",
        conn_pill_cls=conn_pill_cls, conn_pill_text=html.escape(conn_pill_text),
        last_update_text=html.escape(last_update_text),
        next_check_text=html.escape(next_check_text),
        chart_caption=html.escape(chart_caption),
        baseline_price_json=baseline_price_json,
        chart_js_fn=CHART_JS_FN,
        rows="".join(rows), candles_json=candles_json)


def render_tick_page(ts):
    """Full detail for one historical tick - every entry in history/log.jsonl
    already carries its own EvalCandles/Metrics in full, this just surfaces
    what was already being persisted rather than collecting anything new.
    """
    entry = next((e for e in market_agent.history(limit=500)
                  if e.get("receivedAt") == ts), None)
    if entry is None:
        return TICK_PAGE.format(
            ts="not found", chart_block="",
            metric_rows="<tr><td colspan='2'>That tick has aged out of history "
                        "(bounded to the most recent 500) or the link is stale.</td></tr>",
            signal_block="")

    status = entry.get("Status")
    metrics = entry.get("Metrics") or {}
    triggered = bool(metrics.get("Triggered"))
    reasons = ", ".join(metrics.get("Reasons") or []) or "—"

    rows = [
        ("Status", html.escape(str(status or "?"))),
        ("Triggered", "yes" if triggered else "no"),
        ("Reasons", html.escape(reasons)),
        ("Price move", _fmt_pct(metrics.get("PriceMovePercent"))),
        ("Volatility", _fmt_pct(metrics.get("AvgVolatilityPercent"))),
        ("Avg volume", _fmt_num(metrics.get("AvgVolume"))),
        ("Baseline price", metrics.get("BaselinePrice") if metrics.get("BaselinePrice") is not None else "—"),
        ("Baseline avg volume", _fmt_num(metrics.get("BaselineAvgVolume"))),
        ("Baseline set at", _fmt_dt(metrics.get("BaselineTimestamp"))),
        ("Baseline avg volatility", _fmt_pct(metrics.get("BaselineAvgVolatilityPercent"))),
        ("Last triggered", _fmt_dt(metrics.get("LastTriggered"))),
    ]
    # Token usage/cost - only ever present on a Completed (billed) tick, and
    # only once the Workflow Service actually reports them; absent today,
    # shows up here automatically the moment it starts being broadcast, no
    # further change needed on this side.
    input_tokens, output_tokens = entry.get("InputTokens"), entry.get("OutputTokens")
    if input_tokens is not None or output_tokens is not None:
        rows.append(("Tokens (in / out)", f"{_fmt_num(input_tokens)} / {_fmt_num(output_tokens)}"))
        cost = _estimate_cost_usd(entry.get("Model"), input_tokens, output_tokens)
        rows.append(("Estimated cost", cost if cost else "unknown (no rate for this model)"))
    metric_rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    signal = entry.get("Signal")
    if status == "Completed" and signal:
        signal_block = (
            "<h2>Claude analysis</h2>"
            f"<p><b>Question:</b> {html.escape(str(signal.get('Question') or ''))}</p>"
            f"<pre>{html.escape(str(signal.get('Answer') or ''))}</pre>"
        )
    else:
        signal_block = ""

    candles, candles_json, baseline_price_json = _candles_payload(entry)
    caption = (f"{len(candles)} candles for this tick" if len(candles) > 1
               else "No candle data on this tick.")
    chart_block = _render_chart_block(entry, caption)

    return TICK_PAGE.format(
        ts=html.escape(_fmt_dt(entry.get("RunAt")) if entry.get("RunAt") else
                       time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))),
        chart_block=chart_block, metric_rows=metric_rows, signal_block=signal_block)


# Seeded from published pricing at the time this was written - not fetched
# live (Anthropic has no API for that - see ACTION-PLAN.md). Needs manual
# updates if pricing changes or a new model shows up; an unrecognized
# Model just shows "unknown" rather than a guessed number.
_CLAUDE_RATES_PER_MTOK = {
    "claude-opus-4-5": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}


def _estimate_cost_usd(model, input_tokens, output_tokens):
    rate = _CLAUDE_RATES_PER_MTOK.get(model)
    if not rate or input_tokens is None or output_tokens is None:
        return None
    in_rate, out_rate = rate
    cost = (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)
    return f"${cost:.4f}"


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

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _path(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def _query(self, name):
        if "?" not in self.path:
            return None
        for part in self.path.split("?", 1)[1].split("&"):
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def do_GET(self):
        if self._path().endswith("/tick"):
            raw_ts = self._query("ts")
            try:
                ts = float(raw_ts)
            except (TypeError, ValueError):
                ts = None
            return self._send(render_tick_page(ts))
        if self._path().endswith("/saxo-login"):
            # Relayed server-side (see market_agent.resolve_saxo_login_redirect)
            # so the browser never makes a cross-origin request to XWEB_HOST
            # itself - that's what triggers Chrome's Local Network Access
            # prompt when viewing the panel via a public hostname, and it
            # wouldn't even work from outside the LAN regardless of Allow/Deny.
            location = market_agent.resolve_saxo_login_redirect()
            if location:
                return self._redirect(location)
            return self._send(
                "<!doctype html><meta charset='utf-8'>"
                "<p>Could not reach the Workflow Service to start Saxo login. "
                "Check that it's reachable and try again.</p>"
                "<p><a href='./'>&larr; back</a></p>",
                status=502)
        return self._send(render_page())

    def do_POST(self):
        ok, msg = market_agent.trigger_real_run()
        return self._send(render_page(notice=msg, good=ok))


if __name__ == "__main__":
    market_agent.start_background_thread()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
