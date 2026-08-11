#!/usr/bin/env python3
"""Small web UI for the Ansible control node, served through HA ingress.

Ingress means Home Assistant proxies this panel behind its own auth, so no
port is exposed on the LAN and there is no separate login. All paths must be
relative - HA serves the panel under a generated prefix that changes.

One run at a time, enforced with a lock file: two concurrent apt runs against
the same host would fight, and a cordon/drain overlapping an update is worse.
"""
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
LOCK = SHARE / ".run.lock"
RUNNER = "/usr/bin/run-ansible-update.sh"

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
    if not LOCK.exists():
        return None
    try:
        data = json.loads(LOCK.read_text())
    except Exception:
        return None
    pid = data.get("pid")
    # A stale lock after a container restart would block every future run,
    # so verify the process actually exists rather than trusting the file.
    if pid and not Path(f"/proc/{pid}").exists():
        LOCK.unlink(missing_ok=True)
        return None
    return data


def recent_logs(limit=8):
    if not LOGDIR.is_dir():
        return []
    files = sorted(LOGDIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        rc = "?"
        try:
            tail = f.read_text(errors="replace")[-4000:]
            m = re.findall(r"finished \(exit (\d+)\)", tail)
            if m:
                rc = m[-1]
        except OSError:
            pass
        out.append({"name": f.name, "rc": rc})
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
    with _lock:
        if running():
            return False, "A run is already in progress."
        if playbook not in playbooks():
            return False, "Unknown playbook."
        if check and playbook not in PREVIEWABLE:
            return False, "This playbook has no preview mode."
        args = [RUNNER, playbook] + (["--check"] if check else [])
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        LOCK.write_text(json.dumps({"pid": proc.pid, "playbook": playbook, "check": check}))

        def reap():
            proc.wait()
            LOCK.unlink(missing_ok=True)

        threading.Thread(target=reap, daemon=True).start()
        return True, f"Started {'preview of ' if check else ''}{playbook}."


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Ansible Control</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  margin: 0; padding: 16px; background: transparent; }}
h1 {{ font-size: 1.15rem; margin: 0 0 4px; }}
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
  font-size: .85rem; border: 1px solid rgba(127,127,127,.35); }}
.run {{ background: rgba(255,170,0,.16); }}
h2 {{ font-size: .95rem; margin: 22px 0 8px; }}
ul {{ list-style: none; padding: 0; margin: 0; }}
li {{ padding: 6px 0; border-bottom: 1px solid rgba(127,127,127,.18);
  font-size: .82rem; display: flex; gap: 10px; align-items: center; }}
li a {{ color: inherit; }}
.ok {{ color: #2e7d32; font-weight: 600; }}
.bad {{ color: #c62828; font-weight: 600; }}
pre {{ white-space: pre-wrap; word-break: break-word; font-size: .75rem;
  background: rgba(127,127,127,.1); padding: 12px; border-radius: 8px;
  max-height: 60vh; overflow: auto; }}
</style></head><body>
<h1>Ansible Control</h1>
<div class="meta">Playbooks at commit {commit}</div>
{banner}
{cards}
<h2>Recent runs</h2>
<ul>{logs}</ul>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the add-on log for Ansible output, not HTTP noise

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _path(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def do_GET(self):
        p = self._path()
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
            banner = f"<div class='banner'>{html.escape(notice)}</div>"

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
            cls = "ok" if entry["rc"] == "0" else "bad"
            label = "ok" if entry["rc"] == "0" else f"exit {entry['rc']}"
            logs.append(
                "<li><span class='{c}'>{l}</span>"
                "<a href='./log?name={n}'>{n}</a></li>".format(
                    c=cls, l=html.escape(label), n=html.escape(entry["name"])))
        if not logs:
            logs.append("<li>No runs yet.</li>")

        return PAGE.format(commit=html.escape(current_commit()),
                           banner=banner, cards="".join(cards),
                           logs="".join(logs))


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
