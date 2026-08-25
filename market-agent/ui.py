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
from pathlib import Path

import market_agent

PORT = int(os.environ.get("INGRESS_PORT", "8099"))

# osifont-lgpl3fe.woff: https://github.com/hikikomori82/osifont, GNU LGPL v3
# with font exception - embedding it in this page's CSS doesn't put the page
# itself under LGPL, that's what the font exception is for. Same file/route
# pattern as the cluster-control add-on's header font, for the same reason
# (served from its own cached route rather than inlined, so a reload doesn't
# resend ~80 KB every time) and for visual consistency between the two
# add-ons' panels. A missing file just 404s that one request; the font
# stack below still falls back to system-ui/Segoe UI.
FONT_PATH = Path(__file__).parent / "osifont-lgpl3fe.woff"
FONT_FACE = (
    "@font-face { font-family: 'osifont'; src: url(./font.woff) format('woff'); "
    "font-display: swap; }"
    if FONT_PATH.is_file() else ""
)

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


def _auth_pill(entries):
    """Prefers the saxo.authstatus push - pushed the instant a login or
    refresh actually happens, not tied to marketagent.preview's 5-minute
    poll cadence at all. Falls back to inferring it from the latest
    preview tick's own Status (the old, laggier signal) only if no
    saxo.authstatus has arrived yet - an older Workflow Service without
    this topic, or just not received one this run - same
    defensive-fields-may-be-absent pattern as PublicLoginUrl and the
    trigger threshold fields.
    """
    status = market_agent.auth_status()
    if status is not None:
        return ("pill-ok", "Auth: connected") if status.get("Authenticated") \
            else ("pill-warn", "Auth: login required")

    latest_status = entries[-1].get("Status") if entries else None
    if latest_status is None:
        return "pill-unknown", "Auth: unknown"
    if latest_status == "SaxoAuthRequired":
        return "pill-warn", "Auth: login required"
    return "pill-ok", "Auth: connected"


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
    """HH:MM in the container's local time zone, for a candle from today;
    prefixes the date otherwise - without this, stale candles (e.g.
    Friday's last session, still shown through a closed weekend because
    the upstream data source has nothing newer to return) read as if they
    were today's live price action. Confirmed for real 2026-08-23:
    EvalCandles was still 2026-08-21 17:00-20:55 on a Sunday, with no date
    shown anywhere, looking exactly like fresh data.

    _parse_dotnet_dt hands back UTC (the wire format's own zone) -
    astimezone() with no argument converts to the system's local zone,
    the same one time.localtime() (used for "Last update"/"Last check"
    elsewhere on this page) already reads from. Converting here keeps the
    chart's own axis/caption consistent with those, rather than the two
    silently disagreeing by the local UTC offset - found for real
    2026-08-25 when a check timestamped locally as 21:13 had its newest
    candle labelled 19:10, two hours "behind" for no real reason.
    """
    dt = _parse_dotnet_dt(s)
    if not dt:
        return ""
    dt = dt.astimezone()
    if dt.date() == datetime.now().astimezone().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%b %d, %H:%M")


def _fmt_dt(s):
    dt = _parse_dotnet_dt(s)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def _fmt_price(v):
    return f"{v:.4f}" if isinstance(v, (int, float)) else "—"


def _fmt_signed(v, fmt):
    if not isinstance(v, (int, float)):
        return "—"
    return f"+{v:{fmt}}" if v >= 0 else f"{v:{fmt}}"


def _reason_status(name, metrics):
    """Whether a named measure crossed its own threshold this check,
    straight from Reasons - not re-derived from the raw numbers, since
    Reasons is the Workflow Service's own authoritative answer and
    re-deriving it here risks a rounding/edge-case mismatch. Only ever
    called for PriceMove/Volatility - Volume has no threshold to be
    over/under any more (see _render_measure_cards).

    Deliberately says "over threshold"/"under threshold", never
    "triggered" - being over threshold here just means this measure is
    one reason Delta threshold met is "yes" for this check, not that any
    Claude call happened. Every background-loop check is a free preview,
    never billed - only the Run AI Trend Analysis button actually calls
    Claude, so a routine check with Delta threshold met: yes has still
    triggered nothing on its own.
    """
    reasons = metrics.get("Reasons") or []
    return "over threshold" if name in reasons else "under threshold"


def _measure_card(label, value_text, sub_text, extra_cls="", spark_html=""):
    cls = f"measure-card {extra_cls}".strip()
    return (f"<div class='{cls}'><div class='measure-label'>{html.escape(label)}</div>"
            f"<div class='measure-value'>{value_text}</div>"
            f"{spark_html}"
            f"<div class='measure-sub'>{sub_text}</div></div>")


def _measure_status_cls(name, metrics):
    return "hit" if _reason_status(name, metrics).startswith("over") else ""


def _measure_points(history, field):
    """Up to the last 12 checks' worth of `field`, oldest first. No more
    "not evaluated" distinction to fade - every check evaluates fully
    now (there's no more bootstrap/baseline state to be missing).
    """
    points = []
    for e in history[-12:]:
        m = e.get("Metrics") or {}
        v = m.get(field)
        if isinstance(v, (int, float)):
            points.append({"v": v})
    return points


def _measure_spark(history, field, threshold, uid, name):
    """(canvas HTML, draw-call JS) for one measure's tiny trend line, or
    ("", "") if there isn't enough recent history to make a line
    meaningful yet. Self-contained per card (own <script>, own copy of
    CHART_JS_FN) rather than relying on another script block elsewhere on
    the page having already run first - same reasoning _render_chart_block
    already uses, since not every page is guaranteed to have a main chart
    block at all (e.g. a check with no candle data).
    """
    points = _measure_points(history, field)
    if len(points) < 2:
        return "", ""
    canvas_id = f"spark-{name}-{uid}"
    canvas_html = f"<canvas id='{canvas_id}' class='measure-spark'></canvas>"
    js = f"drawSparkline('{canvas_id}', {json.dumps(points)}, {json.dumps(threshold)});"
    return canvas_html, js


def _render_measure_cards(entry, history):
    """Three measures, side by side, each labelled with what it's actually
    compared against, plus a tiny trend line across the last ~12 checks -
    a compact dashboard view of the same distinction _render_trigger_detail's
    fuller tables make explicit. Shared by the main page (latest check,
    under the chart) and the workflow-detail page (top of "How this was
    evaluated") so the two never drift apart. `history` is the chronological
    (oldest-first) list of checks ending at-or-before `entry`, used only for
    the sparklines - the big value/status text still comes from `entry` alone.
    """
    metrics = entry.get("Metrics") or {}
    uid = int(entry.get("receivedAt", 0) * 1000)
    spark_js_parts = []

    def pct_sub(measured, threshold, name):
        if threshold is None:
            return "threshold not reported yet"
        return f"of {_fmt_pct(threshold)} · {_reason_status(name, metrics)}"

    price_spark_html, price_spark_js = _measure_spark(
        history, "PriceMovePercent", metrics.get("PriceMoveThresholdPercent"), uid, "pricemove")
    spark_js_parts.append(price_spark_js)
    price_card = _measure_card(
        "Price move", _fmt_pct(metrics.get("PriceMovePercent")),
        pct_sub(metrics.get("PriceMovePercent"), metrics.get("PriceMoveThresholdPercent"), "PriceMove"),
        _measure_status_cls("PriceMove", metrics), price_spark_html)

    vol_spark_html, vol_spark_js = _measure_spark(
        history, "AvgVolatilityPercent", metrics.get("VolatilityThresholdPercent"), uid, "volatility")
    spark_js_parts.append(vol_spark_js)
    vol_card = _measure_card(
        "Volatility", _fmt_pct(metrics.get("AvgVolatilityPercent")),
        pct_sub(metrics.get("AvgVolatilityPercent"), metrics.get("VolatilityThresholdPercent"), "Volatility"),
        _measure_status_cls("Volatility", metrics), vol_spark_html)

    # Volume is reported but not part of the trigger any more - it isn't
    # compared against anything, so no threshold, no over/under status, no
    # "hit" highlighting. The upstream data source never reports volume at
    # all for OTC/FX-spot and precious-metals instruments (confirmed
    # against xWeb's own CLAUDE.md) - XAGUSD, the only symbol actually
    # configured today, is exactly that, so this card is expected to show
    # "—" in practice.
    avg_vol = metrics.get("AvgVolume")
    volume_sub = "reported, not part of the trigger" if avg_vol is not None else "no volume data for this instrument"
    volume_spark_html, volume_spark_js = _measure_spark(history, "AvgVolume", None, uid, "volume")
    spark_js_parts.append(volume_spark_js)
    volume_card = _measure_card("Volume", _fmt_num(avg_vol), volume_sub, "", volume_spark_html)

    spark_js = "".join(p for p in spark_js_parts if p)
    script_block = f"<script>{CHART_JS_FN}\n{spark_js}</script>" if spark_js else ""
    return f"<div class='measures'>{price_card}{vol_card}{volume_card}</div>{script_block}"


def _render_trigger_detail(entry, history):
    """All three measures are computed *within this window itself* (first
    candle vs last for Price move, the window's own high-low range for
    Volatility) against a fixed threshold - there's no persisted baseline
    anywhere in the trigger logic any more (removed 2026-08-25: it never
    survived a Workflow Service restart anyway, which is what caused the
    original "stuck at FirstRun forever" bug this whole feature grew out
    of - removing the state removed the thing a restart could wipe).
    Volume is reported for reference but doesn't feed the trigger at all.
    """
    metrics = entry.get("Metrics") or {}
    candles = entry.get("EvalCandles") or []

    parts = ["<h2>How this was evaluated</h2>", _render_measure_cards(entry, history)]

    # --- Price move: window-internal, first candle vs last candle ---
    parts.append("<h3>Price move — window detail</h3>")
    if len(candles) >= 2:
        first, last = candles[0], candles[-1]

        def mid(c):
            bid, ask = c.get("CloseBid"), c.get("CloseAsk")
            return (bid + ask) / 2 if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None

        p0, p1 = mid(first), mid(last)
        t0, t1 = _fmt_candle_time(first.get("Time")), _fmt_candle_time(last.get("Time"))
        diff = (p1 - p0) if (p0 is not None and p1 is not None) else None
        # Signed % computed here from p0/p1, NOT metrics["PriceMovePercent"] -
        # that field is always Math.Abs(...) on the Workflow Service side
        # (it's compared against a threshold as a magnitude, direction
        # doesn't matter for triggering), so reusing it here would show
        # "+0.60%" even on a tick where price fell - caught by testing
        # with a real down-move before shipping, not assumed correct.
        pct = (diff / p0 * 100) if (diff is not None and p0) else None
        parts.append(
            "<table class='compare'><tr><th></th><th>Time</th><th class='num'>Price</th></tr>"
            f"<tr><td>Window start</td><td>{html.escape(t0)}</td><td class='num'>{_fmt_price(p0)}</td></tr>"
            f"<tr><td>Window end (current)</td><td>{html.escape(t1)}</td><td class='num'>{_fmt_price(p1)}</td></tr>"
            f"<tr><td>Change</td><td></td><td class='num'>{_fmt_signed(diff, '.4f')} "
            f"({_fmt_signed(pct, '.2f')}%)</td></tr>"
            "</table>")
    else:
        parts.append("<p class='note'>No candle data on this check to compute a window.</p>")

    # --- Volatility: also window-internal ---
    parts.append("<h3>Volatility</h3>")
    parts.append(f"<p>This window's average high–low range: <b>{_fmt_pct(metrics.get('AvgVolatilityPercent'))}</b></p>")
    parts.append("<p class='note'>Computed within this window only - see the card above for the "
                  "threshold it's checked against.</p>")

    # --- Volume: reported, not part of the trigger ---
    parts.append("<h3>Volume</h3>")
    avg_vol = metrics.get("AvgVolume")
    if avg_vol is not None:
        parts.append(f"<p>This window's average volume: <b>{_fmt_num(avg_vol)}</b></p>")
        parts.append("<p class='note'>Reported for reference only - it isn't compared against "
                      "anything and doesn't contribute to Delta threshold met.</p>")
    else:
        parts.append("<p class='note'>No volume data for this instrument (the upstream data "
                      "source doesn't report volume for OTC/FX-spot and precious-metals "
                      "instruments).</p>")

    return "".join(parts)


def _candles_payload(entry):
    """(candles list, candles_json) for one tick - shared by the main page
    (latest tick) and the per-tick detail page (whichever tick you
    clicked through to), so the chart-drawing JS only needs to exist once.
    """
    candles = entry.get("EvalCandles") or []
    candles_json = json.dumps([
        {"CloseBid": k.get("CloseBid", 0), "CloseAsk": k.get("CloseAsk", 0),
         "Time": _fmt_candle_time(k.get("Time"))}
        for k in candles])
    return candles, candles_json


def _chart_caption(entry, symbol):
    """One caption, shared by the main page and Workflow detail, so a
    stale/closed-market chart never gets a different (accidentally more
    honest, or less) caption depending on which page you're looking at.
    Symbol is escaped here since it's the one piece that's user-config,
    not broadcast data - everything else is either static text or a
    safely-formatted timestamp, so the caller must NOT re-escape this
    result (it already contains real HTML: a <strong> around the
    stale-data clause, not just plain text).
    """
    candles = entry.get("EvalCandles") or []
    if len(candles) < 2:
        return "No candle data on this check."
    end_dt = _parse_dotnet_dt(candles[-1].get("Time"))
    start, end = _fmt_candle_time(candles[0].get("Time")), _fmt_candle_time(candles[-1].get("Time"))

    parts = [f"{html.escape(symbol)} mid price (bid/ask average) — "
             f"last {len(candles)} candles, {html.escape(start)}–{html.escape(end)}"]
    if end_dt is not None and end_dt.astimezone().date() != datetime.now().astimezone().date():
        parts.append("<strong>no newer candles since then — market is likely closed</strong>")
    return " · ".join(parts)


# Shared by both PAGE and TICK_PAGE - plain JS, not run through .format()
# itself, so braces here are normal (single), not doubled like the rest
# of this file's templates. Takes a canvas element id plus the candles
# _candles_payload() produces, draws axis labels and a current-price
# marker.
CHART_JS_FN = """
function niceAxisStep(roughStep) {
  const mag = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const residual = roughStep / mag;
  if (residual > 5) return 10 * mag;
  if (residual > 2) return 5 * mag;
  if (residual > 1) return 2 * mag;
  return mag;
}

function drawMarketChart(canvasId, candles) {
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
  let dataMin = Math.min(...vals), dataMax = Math.max(...vals);
  if (dataMax === dataMin) { dataMax += 1; dataMin -= 1; }

  // "Nice" round-number gridlines (Heckbert's algorithm) instead of just
  // labelling the two data endpoints - a real scale needs an even step,
  // not just "here's the top and bottom value".
  const step = niceAxisStep((dataMax - dataMin) / 4);
  const min = Math.floor(dataMin / step) * step;
  const max = Math.ceil(dataMax / step) * step;
  const decimals = Math.max(0, -Math.floor(Math.log10(step)));
  const ticks = [];
  for (let v = min; v <= max + step / 2; v += step) ticks.push(v);

  const x = i => padL + (i / (vals.length - 1)) * (cssW - padL - padR);
  const y = v => cssH - padB - ((v - min) / (max - min)) * (cssH - padT - padB);

  const muted = getComputedStyle(document.body).color;
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'right';
  ticks.forEach(v => {
    ctx.strokeStyle = muted; ctx.globalAlpha = .18; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(cssW - padR, y(v)); ctx.stroke();
    ctx.globalAlpha = 1; ctx.fillStyle = muted;
    ctx.fillText(v.toFixed(decimals), padL - 8, y(v));
  });

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

  // Time axis: real per-candle Time (the price data's own timestamp, from
  // the upstream chart sample), not when this add-on happened to receive
  // the check that carried it - a handful of evenly-spaced labels across the
  // window, not just the two endpoints, so it reads as an actual scale.
  ctx.fillStyle = muted; ctx.globalAlpha = .7; ctx.textBaseline = 'alphabetic';
  const xTickCount = Math.min(6, vals.length);
  const xTickIdx = [...new Set(Array.from({length: xTickCount},
    (_, i) => Math.round(i * (vals.length - 1) / (xTickCount - 1))))];
  xTickIdx.forEach(i => {
    const px = x(i), label = candles[i].Time || '';
    if (!label) return;
    ctx.strokeStyle = muted; ctx.globalAlpha = .12; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, padT); ctx.lineTo(px, cssH - padB); ctx.stroke();
    ctx.globalAlpha = .7; ctx.fillStyle = muted;
    ctx.textAlign = i === 0 ? 'left' : (i === vals.length - 1 ? 'right' : 'center');
    ctx.fillText(label, px, cssH - 4);
  });
}

// Tiny inline trend line for one measure-card - recent values only, no
// axis/labels at all (kept deliberately minimal so the card doesn't grow).
// `points` is [{v}], oldest first. `threshold`, if not null, draws as a
// flat dashed reference line so "how close" is visible at a glance -
// null for Volume, which has no threshold to compare against any more.
function drawSparkline(canvasId, points, threshold) {
  const c = document.getElementById(canvasId);
  if (!c || points.length < 2 || !c.getContext) return;
  const ctx = c.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = c.clientWidth || 120, cssH = 28, padY = 3;
  c.width = cssW * dpr; c.height = cssH * dpr;
  ctx.scale(dpr, dpr);

  const vals = points.map(p => p.v);
  let min = Math.min(...vals), max = Math.max(...vals);
  if (threshold !== null) { min = Math.min(min, threshold); max = Math.max(max, threshold); }
  if (max === min) { max += 1; min -= 1; }
  const x = i => (i / (points.length - 1)) * cssW;
  const y = v => cssH - padY - ((v - min) / (max - min)) * (cssH - padY * 2);

  if (threshold !== null) {
    ctx.strokeStyle = '#b26a00'; ctx.globalAlpha = .5; ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(0, y(threshold)); ctx.lineTo(cssW, y(threshold)); ctx.stroke();
    ctx.setLineDash([]);
  }

  ctx.strokeStyle = '#4a90d9'; ctx.globalAlpha = .9; ctx.lineWidth = 1.4;
  ctx.beginPath();
  vals.forEach((v, i) => i === 0 ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)));
  ctx.stroke();

  const lastIdx = points.length - 1;
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#4a90d9';
  ctx.beginPath(); ctx.arc(x(lastIdx), y(vals[lastIdx]), 2, 0, 7); ctx.fill();
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
{font_face}
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; background: transparent; }}
h1 {{ font-family: 'osifont', system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1.6rem; margin: 0 0 4px; }}
.meta {{ display: flex; align-items: center; flex-wrap: wrap; gap: .5rem 1rem;
  font-size: .8rem; opacity: .7; margin-bottom: 16px; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 12px; }}
button {{ font: inherit; font-weight: 700; font-size: .85rem; padding: 10px 22px;
  border-radius: 999px; border: none; background: #039be5; color: #fff;
  cursor: pointer; }}
button:hover {{ background: #0288d1; }}
.run-row {{ display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }}
.run-row form {{ margin: 0; }}
.hint {{ font-size: .78rem; opacity: .65; margin: 0 0 9px; }}
.banner {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
  font-size: .85rem; border: 1px solid rgba(127,127,127,.35);
  background: rgba(127,127,127,.08); }}
.notice-ok {{ background: rgba(46,125,50,.14); }}
.notice-bad {{ background: rgba(198,40,40,.14); }}
h2 {{ font-size: .95rem; margin: 0 0 10px; }}
h3.section-sub {{ font-size: .78rem; font-weight: 600; text-transform: uppercase;
  letter-spacing: .04em; opacity: .6; margin: 16px 0 8px; }}
canvas {{ width: 100%; height: 220px; display: block; }}
.chart-caption {{ font-size: .78rem; opacity: .65; margin: 6px 0 0; }}
.measures {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; }}
.measure-card {{ border: 1px solid rgba(127,127,127,.25); border-radius: 8px;
  padding: 10px 12px; }}
.measure-card.hit {{ border-color: #b26a00; background: rgba(178,106,0,.1); }}
.measure-label {{ font-size: .72rem; opacity: .65; text-transform: uppercase; letter-spacing: .04em; }}
.measure-value {{ font-size: 1.3rem; font-weight: 700; font-variant-numeric: tabular-nums; margin: 2px 0; }}
.measure-spark {{ width: 100%; height: 28px; display: block; margin: 2px 0; }}
.measure-sub {{ font-size: .74rem; opacity: .7; }}
.ai-result .note {{ font-size: .78rem; opacity: .65; margin: 0 0 8px; }}
.ai-result pre {{ white-space: pre-wrap; word-break: break-word; font-size: .82rem;
  background: rgba(127,127,127,.1); padding: 10px 12px; border-radius: 8px; margin: 6px 0 0; }}
.table-scroll {{ overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid rgba(127,127,127,.18); white-space: nowrap; }}
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
.row-link {{ display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px;
  border-radius: 999px; background: rgba(127,127,127,.14); color: inherit;
  text-decoration: none; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.row-link:hover, .row-link:focus-visible {{ background: rgba(3,155,229,.2); }}
.row-link::after {{ content: "\\2192"; opacity: .55; font-size: .85em; }}
</style></head><body>
<h1>Market Agent</h1>
<div class="meta">
<span>{symbol}</span>
<span class="pill {conn_pill_cls}">{conn_pill_text}</span>
<a class="pill {auth_pill_cls}" href="{auth_login_url}" target="_blank" rel="noopener noreferrer" title="Open login">{auth_pill_text}</a>
<span>{last_update_text}</span>
<span>{next_check_text}</span>
</div>
{banner}
<div class="card">
<canvas id="chart" width="900" height="220"></canvas>
<p class="chart-caption">{chart_caption}</p>
{last_check_block}
</div>
<div class="card">
<h2>AI Analysis</h2>
<div class="run-row">
<form method="post" action="./run"><button type="submit">Run AI Trend Analysis</button></form>
<span class="hint">Billed Claude call</span>
</div>
<div class="ai-result">
{ai_result_block}
</div>
</div>
<div class="card">
<h2>Workflow History</h2>
<div class="table-scroll">
<table><tr><th>Time</th><th>Status</th><th>Delta threshold met</th><th>Reasons</th>
<th class="num">Price move</th><th class="num">Volatility</th><th class="num">Volume</th></tr>
{rows}
</table>
</div>
</div>
<script>
{chart_js_fn}
drawMarketChart('chart', {candles_json});
</script>
</body></html>"""


TICK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Market Agent - Workflow detail</title>
<style>
{font_face}
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; max-width: 700px; margin-inline: auto; }}
h1 {{ font-family: 'osifont', system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1.3rem; margin: 0 0 4px; }}
.back {{ font-size: .85rem; opacity: .7; margin-bottom: 12px; display: inline-block; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 14px; }}
canvas {{ width: 100%; height: 220px; display: block; }}
table {{ width: 100%; border-collapse: collapse; font-size: .85rem; margin-bottom: 14px; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(127,127,127,.18); }}
td.num {{ font-variant-numeric: tabular-nums; }}
th {{ width: 40%; opacity: .7; font-weight: 600; }}
table.compare th {{ width: auto; opacity: 1; }}
table.compare td.num, table.compare th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
h2 {{ font-size: .95rem; margin: 20px 0 8px; }}
h3 {{ font-size: .85rem; margin: 16px 0 6px; opacity: .85; }}
.note {{ font-size: .78rem; opacity: .65; margin: -8px 0 14px; }}
.measures {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 14px; }}
.measure-card {{ border: 1px solid rgba(127,127,127,.25); border-radius: 8px;
  padding: 10px 12px; }}
.measure-card.hit {{ border-color: #b26a00; background: rgba(178,106,0,.1); }}
.measure-label {{ font-size: .72rem; opacity: .65; text-transform: uppercase; letter-spacing: .04em; }}
.measure-value {{ font-size: 1.3rem; font-weight: 700; font-variant-numeric: tabular-nums; margin: 2px 0; }}
.measure-spark {{ width: 100%; height: 28px; display: block; margin: 2px 0; }}
.measure-sub {{ font-size: .74rem; opacity: .7; }}
pre {{ white-space: pre-wrap; word-break: break-word; font-size: .8rem;
  background: rgba(127,127,127,.1); padding: 10px 12px; border-radius: 8px; }}
</style></head><body>
<a class="back" href="./">&larr; back to Market Agent</a>
<h1>Workflow detail — {ts}</h1>
{chart_block}
<table>
{metric_rows}
</table>
{trigger_detail}
{signal_block}
</body></html>"""


def _render_chart_block(entry, caption):
    """`caption` is already-safe HTML from _chart_caption() - not
    re-escaped here, same reasoning as PAGE's own chart_caption slot.
    """
    candles, candles_json = _candles_payload(entry)
    if len(candles) < 2:
        return f"<p>{caption}</p>"
    return (
        "<div class=\"card\"><canvas id=\"chart\"></canvas></div>"
        f"<p style=\"font-size:.8rem;opacity:.65;margin-top:-6px\">{caption}</p>"
        f"<script>{CHART_JS_FN}\ndrawMarketChart('chart', {candles_json});</script>"
    )


def _render_ai_result_block(entry):
    """The most recent *billed* (Completed) run's question/answer, shown
    directly on the main page under the Run AI Trend Analysis button - not just
    on that check's own Workflow detail page. `entry` is the latest
    Completed entry in history, or None if no billed run has happened yet
    (routine Preview/TriggerNotMet/MarketClosed ticks never carry a
    Signal, so scanning for one specifically is required - the overall
    latest history entry is very rarely this one).
    """
    if entry is None:
        return "<p class='note'>No AI analysis has run yet - it runs automatically when a " \
               "check's threshold is met, or on demand with the button above.</p>"

    signal = entry.get("Signal") or {}
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("receivedAt", 0)))
    input_tokens, output_tokens = entry.get("InputTokens"), entry.get("OutputTokens")
    cost = _estimate_cost_usd(entry.get("Model"), input_tokens, output_tokens)

    meta_bits = [html.escape(ts)]
    if input_tokens is not None or output_tokens is not None:
        meta_bits.append(f"{_fmt_num(input_tokens)} in / {_fmt_num(output_tokens)} out tokens")
    if cost:
        meta_bits.append(cost)
    meta_bits.append(f"<a href='./tick?ts={entry.get('receivedAt', 0)}'>full workflow detail</a>")

    return (
        f"<p class='note'>{' · '.join(meta_bits)}</p>"
        f"<p><b>Question:</b> {html.escape(str(signal.get('Question') or ''))}</p>"
        f"<pre>{html.escape(str(signal.get('Answer') or ''))}</pre>"
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
        banner = "<div class='banner'>No checks received yet - waiting on market agent workflow service's first broadcast.</div>"

    latest_status = entries[-1].get("Status") if entries else None
    auth_pill_cls, auth_pill_text = _auth_pill(entries)

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
            "<tr><td><a class='row-link' href='./tick?ts={raw_ts}'>{ts}</a></td><td class='{cls}'>{status}</td>"
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
        rows.append("<tr><td colspan='7'>No checks yet.</td></tr>")

    latest_entry = entries[-1] if entries else {}
    candles, candles_json = _candles_payload(latest_entry)
    chart_caption = _chart_caption(latest_entry, market_agent.SYMBOL)

    if entries:
        last_check_when = time.strftime("%H:%M", time.localtime(latest_entry.get("receivedAt", 0)))
        last_check_block = (
            f"<h3 class='section-sub'>Last check — {html.escape(str(latest_status or '?'))} at {last_check_when}</h3>"
            + _render_measure_cards(latest_entry, entries))
    else:
        last_check_block = ""

    latest_completed = next((e for e in reversed(entries) if e.get("Status") == "Completed"), None)
    ai_result_block = _render_ai_result_block(latest_completed)

    return PAGE.format(
        symbol=html.escape(market_agent.SYMBOL), banner=banner,
        auth_pill_cls=auth_pill_cls, auth_pill_text=html.escape(auth_pill_text),
        auth_login_url="./auth-login",
        conn_pill_cls=conn_pill_cls, conn_pill_text=html.escape(conn_pill_text),
        last_update_text=html.escape(last_update_text),
        next_check_text=html.escape(next_check_text),
        chart_caption=chart_caption,
        last_check_block=last_check_block,
        ai_result_block=ai_result_block,
        chart_js_fn=CHART_JS_FN, font_face=FONT_FACE,
        rows="".join(rows), candles_json=candles_json)


def _last_triggered_before(history, ts):
    """The most recent prior check where Delta threshold met was yes -
    computed client-side now that LastTriggered no longer exists on the
    broadcast payload at all (removed along with the rest of the
    persisted trigger state, 2026-08-25). We already retain the history
    needed to derive this ourselves, so the Workflow Service doesn't
    need to track it too.
    """
    prior = [e for e in history if e.get("receivedAt", 0) < ts
             and bool((e.get("Metrics") or {}).get("Triggered"))]
    return max(prior, key=lambda e: e.get("receivedAt", 0)) if prior else None


def render_tick_page(ts):
    """Full detail for one historical tick - every entry in history/log.jsonl
    already carries its own EvalCandles/Metrics in full, this just surfaces
    what was already being persisted rather than collecting anything new.
    """
    full_history = market_agent.history(limit=200)
    entry = next((e for e in full_history if e.get("receivedAt") == ts), None)
    if entry is None:
        return TICK_PAGE.format(
            ts="not found", chart_block="", trigger_detail="", font_face=FONT_FACE,
            metric_rows="<tr><td colspan='2'>That check has aged out of history "
                        "(bounded to the most recent 50 checks, plus 20 login-required "
                        "ones) or the link is stale.</td></tr>",
            signal_block="")

    status = entry.get("Status")
    metrics = entry.get("Metrics") or {}
    triggered = bool(metrics.get("Triggered"))
    reasons = ", ".join(metrics.get("Reasons") or []) or "—"

    last_triggered = _last_triggered_before(full_history, ts)
    if last_triggered:
        lt_ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_triggered.get("receivedAt", 0)))
        last_triggered_html = f"<a href='./tick?ts={last_triggered.get('receivedAt', 0)}'>{html.escape(lt_ts)}</a>"
    else:
        last_triggered_html = "—"

    rows = [
        ("Status", html.escape(str(status or "?"))),
        ("Delta threshold met", "yes" if triggered else "no"),
        ("Reasons", html.escape(reasons)),
        ("Last threshold met before this", last_triggered_html),
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

    chart_block = _render_chart_block(entry, _chart_caption(entry, market_agent.SYMBOL))
    history_up_to_entry = [e for e in full_history if e.get("receivedAt", 0) <= ts]
    trigger_detail = _render_trigger_detail(entry, history_up_to_entry)

    return TICK_PAGE.format(
        ts=html.escape(_fmt_dt(entry.get("RunAt")) if entry.get("RunAt") else
                       time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))),
        chart_block=chart_block, metric_rows=metric_rows, font_face=FONT_FACE,
        trigger_detail=trigger_detail, signal_block=signal_block)


# Seeded from published pricing at the time this was written - not fetched
# live (Anthropic has no API for that - see ACTION-PLAN.md). Needs manual
# updates if pricing changes or a new model shows up; an unrecognized
# Model just shows "unknown" rather than a guessed number.
#
# claude-opus-4-8: xWeb's actual default (Claude:Model config) - checked
# against Anthropic's own docs (platform.claude.com), same rate as Opus
# 4.5. A dateless pinned snapshot, not an evergreen alias, so this rate
# won't silently drift out from under a fixed model the way it could for
# a "-latest"-style name.
_CLAUDE_RATES_PER_MTOK = {
    "claude-opus-4-8": (5.00, 25.00),
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

    def _send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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
        if self._path().endswith("/font.woff"):
            try:
                data = FONT_PATH.read_bytes()
            except OSError:
                return self._send("Not found.", status=404, ctype="text/plain")
            # Immutable: the filename would change if the font ever did.
            return self._send(data, ctype="font/woff",
                               headers={"Cache-Control": "public, max-age=31536000, immutable"})
        if self._path().endswith("/tick"):
            raw_ts = self._query("ts")
            try:
                ts = float(raw_ts)
            except (TypeError, ValueError):
                ts = None
            return self._send(render_tick_page(ts))
        if self._path().endswith("/auth-login"):
            # Relayed server-side (see market_agent.resolve_auth_login_redirect)
            # so the browser never makes a cross-origin request to XWEB_HOST
            # itself - that's what triggers Chrome's Local Network Access
            # prompt when viewing the panel via a public hostname, and it
            # wouldn't even work from outside the LAN regardless of Allow/Deny.
            location = market_agent.resolve_auth_login_redirect()
            if location:
                return self._redirect(location)
            return self._send(
                "<!doctype html><meta charset='utf-8'>"
                "<p>Could not reach the Workflow Service to start login. "
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
