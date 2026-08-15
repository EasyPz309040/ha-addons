#!/usr/bin/env python3
"""Small web UI for the Ansible control node, served through HA ingress.

Ingress means Home Assistant proxies this panel behind its own auth, so no
port is exposed on the LAN and there is no separate login. All paths must be
relative - HA serves the panel under a generated prefix that changes.

One run at a time, enforced with a flock in run-ansible-update.sh - that's
the script both cron and this UI invoke, so it's the one place able to
serialize scheduled runs against button clicks. This module only takes an
advisory look at that same lock file to show status; it does not own it.
"""
import fcntl
import html
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("INGRESS_PORT", "8099"))
SHARE = Path("/share/ansible")
LOGDIR = SHARE / "logs"
DIRFILE = SHARE / ".playbook_dir"
LOCK = SHARE / ".run.lock"       # flock target only - never holds content
STATUS = SHARE / ".run.status"   # JSON written by the current/last run
RUNNER = "/usr/bin/run-ansible-update.sh"

# osifont-lgpl3fe.woff: https://github.com/hikikomori82/osifont, GNU LGPL v3
# with font exception - embedding it in this page's CSS doesn't put the page
# itself under LGPL, that's what the font exception is for. Copied alongside
# ui.py in the image and served from its own cached route (./font.woff)
# rather than inlined as a data URI - this page gets reloaded a lot while a
# run is in progress, and inlining would resend the ~80 KB font on every one
# of those reloads instead of once. A missing file just 404s that one
# request; the font stack below still falls back to system-ui/Segoe UI.
FONT_PATH = Path(__file__).parent / "osifont-lgpl3fe.woff"
FONT_FACE = (
    "@font-face { font-family: 'osifont'; src: url(./font.woff) format('woff'); "
    "font-display: swap; }"
    if FONT_PATH.is_file() else ""
)

# Same file the Supervisor renders as this add-on's separate Documentation
# tab, served here too via ./docs so it's reachable without leaving the
# panel - that tab's own URL has already moved once across HA versions (see
# CLAUDE.md gotchas), so linking to it directly from here would be fragile.
DOCS_PATH = Path(__file__).parent / "DOCS.md"

# Playbooks that must never be triggered from a web button. Empty for now -
# provision-cluster.yml used to be here, but only its very first run against a
# brand-new image (before the ansible user exists) needs the interactive
# --ask-pass terminal invocation. Every run after that, including
# self-healing a dead node, is button-safe.
EXCLUDED = set()

# Playbooks that also get a Preview button, which runs `--check --diff`
# first so nothing changes until you click Run. provision-cluster.yml detects
# and repairs whatever is out of spec, so previewing it before a real run is
# worth the extra click; the others are narrower and less surprising.
PREVIEWABLE = {"provision-cluster.yml"}

# Scraped straight from ansible-playbook's default callback output - no
# custom callback plugin, so no Galaxy collection needed to get it. A host
# offline for its own connection fails its first task with this exact line;
# a host whose *delegated* k3s check target (the control plane) is offline
# doesn't produce this, which is exactly why provision-cluster.yml sets
# ignore_unreachable on those tasks instead of letting them abort the host.
UNREACHABLE_RE = re.compile(r'^fatal: \[([^\]]+)\]: UNREACHABLE!', re.MULTILINE)
# Every "Plan: ..." / "Note ..." task in provision-cluster.yml is a debug
# task, which the default callback renders as `ok: [host] => { "msg": "..." }`.
MSG_BLOCK_RE = re.compile(
    r'^(?:ok|changed): \[([^\]]+)\] => \{\s*\n\s*"msg":\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE)
RECAP_RE = re.compile(
    r'^(\S+)\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)',
    re.MULTILINE)


def parse_preview(text):
    """Turn a --check --diff log into {unreachable: [...], plans: {host: [msg,...]}}."""
    unreachable = sorted(set(UNREACHABLE_RE.findall(text)))
    plans = {}
    for host, msg in MSG_BLOCK_RE.findall(text):
        msg = msg.replace('\\"', '"').replace('\\\\', '\\')
        plans.setdefault(host, []).append(msg)
    return {"unreachable": unreachable, "plans": plans}


def classify_run(tail_text, rc):
    """(css_class, label) for a finished run, from its exit code and recap.

    Exit 3 is Ansible's "some hosts unreachable" code. If nothing actually
    failed and the only issue is hosts being offline, that's routine for a
    home fleet where not everything is always powered on - not a failure,
    so it gets its own neutral status rather than the red "bad" one.
    """
    if rc == "0":
        return "ok", "ok"
    if rc == "3":
        recap = RECAP_RE.findall(tail_text)
        if recap and all(int(failed) == 0 for *_, failed in recap):
            offline = [h for h, _ok, _ch, un, _fa in recap if int(un) > 0]
            if offline:
                return "partial", f"{len(offline)} host(s) offline"
    return "bad", f"exit {rc}"


DESCRIPTIONS = {
    "provision-cluster.yml": (
        "Detects and fixes drift fleet-wide: identity, packages, boot config, "
        "k3s membership, the pi2 NFS export, OLED/fan hardware. Self-healing "
        "and safe to re-run any time, including to repair a node that died — "
        "Preview first to see what it would change."
    ),
    "cluster-update.yml": (
        "Cordon, drain, patch, reboot, wait for Ready, uncordon — one host at "
        "a time, workers first, control plane last. The only OS-patching "
        "playbook; this is what the weekly schedule runs by default."
    ),
    "backup-datastore.yml": (
        "Stops k3s, archives the SQLite datastore and TLS material, restarts, "
        "fetches to /share. Placeholder: written but not yet verified to "
        "actually restore from — don't treat a green run as a tested backup."
    ),
    "backup-secrets.yml": (
        "Dumps every Kubernetes Secret (except SA tokens) to /share, live, "
        "no downtime. Workloads and OS state already rebuild from git — "
        "Secrets are the one thing that doesn't, so this is the backup that "
        "actually matters for recovering a dead control plane."
    ),
    "run-command.yml": (
        "Ad-hoc command across the fleet. This button will just fail with "
        "\"No command provided\" — it needs a cmd variable the panel can't "
        "supply. See DOCS.md for a command cookbook; run from a terminal."
    ),
}

_lock = threading.Lock()


def playbook_dir():
    try:
        return Path(DIRFILE.read_text().strip())
    except OSError:
        return None


def playbooks():
    d = playbook_dir()
    if not d or not d.is_dir():
        return []
    names = sorted(p.name for p in d.glob("*.yml"))
    return [n for n in names if n not in EXCLUDED]


def current_commit():
    d = playbook_dir()
    if not d:
        return "unknown"
    try:
        out = subprocess.run(
            ["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def running():
    """Best-effort read of run-ansible-update.sh's lock, for display only.

    Tries to take the same flock the script holds for the run's duration:
    getting it means nothing is running (release it again immediately),
    failing to get it means a run is in progress - authoritative, no pid
    file or /proc check needed, and it can't go stale across a container
    restart the way a recorded pid could.
    """
    if not LOCK.exists():
        return None
    try:
        f = open(LOCK)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return json.loads(STATUS.read_text())
            except Exception:
                return {"playbook": "unknown", "check": False}
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return None
    finally:
        f.close()


def recent_logs(limit=8):
    if not LOGDIR.is_dir():
        return []
    files = sorted(LOGDIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        rc = "?"
        tail = ""
        try:
            tail = f.read_text(errors="replace")[-4000:]
            m = re.findall(r"finished \(exit (\d+)\)", tail)
            if m:
                rc = m[-1]
        except OSError:
            pass
        cls, label = ("?", "?") if rc == "?" else classify_run(tail, rc)
        out.append({"name": f.name, "rc": rc, "cls": cls, "label": label})
    return out


def log_text(name, tail_bytes=60000):
    f = LOGDIR / name
    if not f.is_file() or f.parent != LOGDIR:
        return "Log not found."
    try:
        return f.read_text(errors="replace")[-tail_bytes:]
    except OSError as e:
        return f"Could not read log: {e}"


def start_run(playbook, check=False):
    # This check is just a fast, friendly "already running" message - the
    # real enforcement is the flock run-ansible-update.sh takes on LOCK
    # itself. A run started here can still lose a race to a cron-triggered
    # one between this check and the script actually acquiring the lock;
    # in that rare case the script exits 99 and logs why, and this call
    # reports "Started" optimistically. Recent runs / the log show the truth.
    with _lock:
        if running():
            return False, "A run is already in progress."
        if playbook not in playbooks():
            return False, "Unknown playbook."
        if check and playbook not in PREVIEWABLE:
            return False, "This playbook has no preview mode."
        args = [RUNNER, playbook] + (["--check"] if check else [])
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True, f"Started {'preview of ' if check else ''}{playbook}."


def render_preview_summary(name, summary):
    plans, unreachable = summary["plans"], summary["unreachable"]
    parts = ["<b>Last preview</b>"]
    if plans:
        def strip_host(host, msg):
            # provision-cluster.yml's Plan/Note messages lead with
            # "<hostname>: " for readability in the raw log, where the
            # surrounding `ok: [host] =>` doesn't stand out as clearly as
            # it does here next to the bolded host label - drop the
            # now-redundant repeat.
            prefix = host + ": "
            return msg[len(prefix):] if msg.startswith(prefix) else msg
        items = "".join(
            f"<li><b>{html.escape(h)}</b>: {html.escape(strip_host(h, m))}</li>"
            for h in sorted(plans) for m in plans[h])
        parts.append(f"<ul>{items}</ul>")
    else:
        parts.append("<p>No planned changes on any reachable host.</p>")
    if unreachable:
        parts.append(
            "<p class='unreachable'>Offline, not previewed (not a failure): "
            + html.escape(", ".join(unreachable)) + "</p>")
    parts.append(f"<p><a href='./log?name={html.escape(name)}'>View full log</a></p>")
    return "<div class='card summary'><div class='txt'>" + "".join(parts) + "</div></div>"


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Ansible Cluster Control</title>
<style>
{font_face}
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; background: transparent; }}
h1 {{ font-family: 'osifont', system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 1.6rem; margin: 0 0 4px; }}
.meta {{ opacity: .7; font-size: .8rem; margin-bottom: 16px; }}
.card {{ border: 1px solid rgba(127,127,127,.3); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 10px; display: flex; gap: 12px;
  align-items: center; flex-wrap: wrap; }}
.card .txt {{ flex: 1 1 260px; min-width: 0; }}
.card b {{ font-size: .95rem; }}
.card p {{ margin: 2px 0 0; font-size: .8rem; opacity: .75; }}
button {{ font: inherit; padding: 7px 16px; border-radius: 7px;
  border: 1px solid rgba(127,127,127,.4); background: rgba(127,127,127,.12);
  cursor: pointer; }}
button:hover:not(:disabled) {{ background: rgba(127,127,127,.25); }}
button:disabled {{ opacity: .45; cursor: not-allowed; }}
.banner {{ padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
  font-size: .85rem; border: 1px solid rgba(127,127,127,.35);
  background: rgba(127,127,127,.08); }}
.run {{ background: rgba(255,170,0,.16); }}
.notice-ok {{ background: rgba(46,125,50,.14); }}
.notice-bad {{ background: rgba(198,40,40,.14); }}
h2 {{ font-size: .95rem; margin: 22px 0 8px; }}
ul {{ list-style: none; padding: 0; margin: 0; }}
li {{ padding: 6px 0; border-bottom: 1px solid rgba(127,127,127,.18);
  font-size: .82rem; display: flex; gap: 10px; align-items: center; }}
li a {{ color: inherit; }}
.ok {{ color: #2e7d32; font-weight: 600; }}
.bad {{ color: #c62828; font-weight: 600; }}
.partial {{ color: #b26a00; font-weight: 600; }}
.summary ul {{ list-style: disc; padding-left: 18px; margin: 6px 0 0; }}
.summary li {{ display: list-item; border: none; padding: 2px 0; font-size: .82rem; }}
.unreachable {{ opacity: .8; font-size: .82rem; margin: 10px 0 0; }}
pre {{ white-space: pre-wrap; word-break: break-word; font-size: .75rem;
  background: rgba(127,127,127,.1); padding: 12px; border-radius: 8px;
  max-height: 60vh; overflow: auto; }}
</style></head><body>
<h1>Ansible Cluster Control</h1>
<div class="meta">Playbooks at commit {commit} &middot; <a href="./docs">Docs</a></div>
{banner}
{preview_summary}
{cards}
<h2>Recent runs</h2>
<ul>{logs}</ul>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the add-on log for Ansible output, not HTTP noise

    def _send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _path(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def do_GET(self):
        p = self._path()
        if p.endswith("/font.woff"):
            try:
                data = FONT_PATH.read_bytes()
            except OSError:
                return self._send("Not found.", status=404, ctype="text/plain")
            # Immutable: the filename would change if the font ever did.
            # Safe for the browser to cache indefinitely.
            return self._send(data, ctype="font/woff",
                               headers={"Cache-Control": "public, max-age=31536000, immutable"})
        if p.endswith("/docs"):
            try:
                text = DOCS_PATH.read_text(errors="replace")
            except OSError:
                text = "DOCS.md not found in this image."
            body = ("<!doctype html><meta charset='utf-8'>"
                    "<style>body{font-family:system-ui;padding:16px;max-width:780px;margin:0 auto}"
                    "pre{white-space:pre-wrap;word-break:break-word;font-size:.8rem;"
                    "background:rgba(127,127,127,.1);padding:12px;border-radius:8px}"
                    "</style><p><a href='./'>&larr; back</a></p><pre>"
                    + html.escape(text) + "</pre>")
            return self._send(body)
        if p.endswith("/log"):
            name = ""
            if "?" in self.path:
                q = self.path.split("?", 1)[1]
                for part in q.split("&"):
                    if part.startswith("name="):
                        name = part[5:]
            body = ("<!doctype html><meta charset='utf-8'>"
                    "<style>body{font-family:system-ui;padding:16px}"
                    "pre{white-space:pre-wrap;word-break:break-word;font-size:.75rem;"
                    "background:rgba(127,127,127,.1);padding:12px;border-radius:8px}"
                    "</style><p><a href='./'>&larr; back</a></p><pre>"
                    + html.escape(log_text(name)) + "</pre>")
            return self._send(body)
        return self._send(self.render())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        playbook = ""
        mode = ""
        for part in body.split("&"):
            if part.startswith("playbook="):
                playbook = part[9:].replace("%2E", ".")
            elif part.startswith("mode="):
                mode = part[5:]
        ok, msg = start_run(playbook, check=(mode == "preview"))
        self._send(self.render(notice=msg, good=ok))

    def render(self, notice=None, good=True):
        run = running()
        banner = ""
        if run:
            verb = "Previewing" if run.get("check") else "Running"
            banner = (f"<div class='banner run'><b>{verb}:</b> "
                      f"{html.escape(run.get('playbook', '?'))} — "
                      f"reload this page to check progress, or open the log below.</div>")
        elif notice:
            cls = "banner " + ("notice-ok" if good else "notice-bad")
            banner = f"<div class='{cls}'>{html.escape(notice)}</div>"

        preview_summary = ""
        if not run:
            latest = recent_logs(limit=1)
            if latest and "-preview-" in latest[0]["name"]:
                summary = parse_preview(log_text(latest[0]["name"]))
                preview_summary = render_preview_summary(latest[0]["name"], summary)

        cards = []
        pbs = playbooks()
        if not pbs:
            cards.append("<div class='card'><div class='txt'><b>No playbooks found</b>"
                         "<p>The git sync may have failed — check the add-on log.</p>"
                         "</div></div>")
        for name in pbs:
            desc = DESCRIPTIONS.get(name, "")
            dis = " disabled" if run else ""
            n = html.escape(name)
            buttons = ""
            if name in PREVIEWABLE:
                buttons += (
                    "<form method='post'><input type='hidden' name='playbook' value='{n}'>"
                    "<input type='hidden' name='mode' value='preview'>"
                    "<button type='submit'{dis}>Preview</button></form>".format(n=n, dis=dis))
            buttons += (
                "<form method='post'><input type='hidden' name='playbook' value='{n}'>"
                "<button type='submit'{dis}>Run</button></form>".format(n=n, dis=dis))
            cards.append(
                "<div class='card'><div class='txt'><b>{n}</b><p>{d}</p></div>"
                "{buttons}</div>".format(n=n, d=html.escape(desc), buttons=buttons))

        logs = []
        for entry in recent_logs():
            logs.append(
                "<li><span class='{c}'>{l}</span>"
                "<a href='./log?name={n}'>{n}</a></li>".format(
                    c=entry["cls"], l=html.escape(entry["label"]), n=html.escape(entry["name"])))
        if not logs:
            logs.append("<li>No runs yet.</li>")

        return PAGE.format(font_face=FONT_FACE, commit=html.escape(current_commit()),
                           banner=banner, preview_summary=preview_summary,
                           cards="".join(cards), logs="".join(logs))


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
