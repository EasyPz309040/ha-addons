# EasyPz Ansible Control Node

Runs Ansible playbooks against a home fleet on a schedule. The playbooks
themselves are **not** in this add-on — they're cloned from a separate
private repository at container start, so editing one is a commit and a
restart rather than an image rebuild.

Home Assistant is deliberately the control node because it sits *outside*
the k3s cluster it manages, so it can cordon, drain and reboot cluster
members without rebooting itself.

## Setup

### 1. Fleet SSH key

Authenticates to the managed hosts. Generate it in the Terminal add-on:

```
mkdir -p /share/ansible/.ssh
ssh-keygen -t ed25519 -f /share/ansible/.ssh/id_ansible -C "ansible@haos" -N ""
cat /share/ansible/.ssh/id_ansible.pub
```

Add that public key to `~/.ssh/authorized_keys` for the `ansible` user on
every managed host, and give that user passwordless sudo.

Then seed the host keys, so unattended runs can't hang on a prompt:

```
ssh-keyscan -H 192.168.0.100 192.168.0.101 192.168.0.102 192.168.0.103 \
  >> /share/ansible/.ssh/known_hosts
```

### 2. Deploy key for the playbook repo

A **separate** key, read-only, used only to fetch playbooks:

```
ssh-keygen -t ed25519 -f /share/ansible/.ssh/id_deploy -C "haos-deploy" -N ""
cat /share/ansible/.ssh/id_deploy.pub
```

Add that public key to the private repo: **Settings → Deploy keys → Add
deploy key**. Leave *Allow write access* **unchecked**.

Two keys rather than one because they have different jobs and different blast
radii. A leaked deploy key exposes one repo read-only; a leaked fleet key is
root on every host.

### 3. Configure and start

Set `playbook_repo` to the SSH URL of the private repo
(`git@github.com:USER/REPO.git` — not the HTTPS form, which won't use the
deploy key), then start the add-on and check the log. It lists the playbooks
it found.

## Using it

The add-on adds an **Ansible** entry to the Home Assistant sidebar (served
through ingress, so it sits behind HA's own login and exposes no port on the
LAN).

The panel lists every playbook found in the repo with a **Run** button, shows
the commit they're currently at, and lists recent runs with their exit status.
Click a run to read its full log.

Only one run happens at a time — buttons disable while something is in
progress. Two concurrent apt runs against the same host would fight, and a
cordon/drain overlapping an update is worse.

`provision-cluster.yml` gets a **Preview** button alongside **Run** — Preview
runs `ansible-playbook --check --diff` and changes nothing, so you can see
what it thinks is wrong before letting it fix anything. The one thing
neither button can do is bootstrap a completely fresh image: the very first
run against a host needs an interactive password prompt (`--ask-pass`), so
that one step still has to happen from a terminal. Every run after that,
including repairing a node that died, is button-safe.

## Playbooks

What each button does, and when you'd actually click it.

**`provision-cluster.yml`** — Detects and fixes drift: the `ansible` user and
its key, base packages, filesystem/kernel/boot config, k3s cluster
membership, the pi2 NFS export, and the OLED/fan hardware. Everything reads
current state first and only changes what's actually wrong. **When to use:**
this is the answer to "a node died" or "something's out of spec" — click
**Preview** first to see what it would change, then **Run**. Not scheduled;
there's no routine reason to run it unless you suspect drift or just changed
something (wiring, the drive, inventory).

**`cluster-update.yml`** — Cordon → drain → patch → reboot → wait for
`Ready` → uncordon. Workers first, control plane last; a failure still
uncordons. **When to use:** this is what the scheduled run (`update_playbook`,
weekly by default) does — click it yourself between schedules if you want
patches sooner. It's the only OS-patching playbook; every host here is a k3s
member, so cordon/drain is never wasted effort.

**`backup-datastore.yml`** — Stops k3s, archives the SQLite datastore and
TLS material, restarts, fetches the archive to `/share`. **When to use:**
runs nightly on its own schedule (`backup_schedule`); click it manually right
before anything risky to the control plane — a `cluster-update.yml` run
against pi1, or hand-editing k3s config — so there's a fresh restore point
first. This is the only thing standing between a dead control plane and
rebuilding from scratch, since a single-server (SQLite) cluster gets no
automatic etcd snapshots.

> **Placeholder, not a verified backup.** The logic is written and runs
> clean, but nobody has yet taken one of its archives and actually restored
> a control plane from it. A green run here means "the tar command
> succeeded," not "this is a tested recovery path." Treat it as
> aspirational until that's been exercised for real — see the backlog in
> `ACTION-PLAN.md` in the private repo.

**`run-command.yml`** — Ad-hoc command across the fleet, needs a `cmd`
variable. **The Run button won't do anything useful here** — the panel
posts no variables, so it just fails with "No command provided." Use it from
a terminal instead:

```
docker exec -it <container> ansible-playbook run-command.yml -e "cmd=uptime"
```

### `run-command.yml` cookbook

All of these follow the same shape —
`ansible-playbook run-command.yml -e "cmd=<command>"`, add
`-e "use_shell=true"` for anything with a pipe or redirect, and
`--limit <host>` to target one host instead of the fleet:

| What | Command |
|---|---|
| Disk space on every host | `-e "cmd=df -h /"` |
| k3s node status | `-e "cmd=/usr/local/bin/k3s kubectl get nodes -o wide"` --limit pi1 |
| Is k3s actually running here? | `-e "cmd=systemctl is-active k3s k3s-agent" -e "become_cmd=false"` |
| Pods stuck or crashlooping | `-e "cmd=/usr/local/bin/k3s kubectl get pods -A --field-selector=status.phase!=Running"` --limit pi1 |
| NFS mount actually present | `-e "cmd=mount \| grep nfs" -e "use_shell=true"` |
| Confirm the NFS export | `-e "cmd=showmount -e 192.168.0.102"` --limit pi1 |
| iptables backend in use | `-e "cmd=update-alternatives --query iptables"` |
| Current fsck settings | `-e "cmd=tune2fs -l /dev/mmcblk0p2" -e "use_shell=true"` (adjust device per host) |
| Reboot pending? | `-e "cmd=test -f /var/run/reboot-required && echo yes \|\| echo no" -e "use_shell=true"` |
| OLED/fan service status | `-e "cmd=systemctl status oled-status" -e "become_cmd=false"` |
| Recent journal for k3s | `-e "cmd=journalctl -u k3s --since '1 hour ago' --no-pager" -e "use_shell=true"` |
| Free memory | `-e "cmd=free -h"` |
| Uptime and load, whole fleet | `-e "cmd=uptime"` |

The `-o wide`/`showmount` examples only make sense against pi1 (the control
plane) — use `--limit pi1` or they'll fail loudly on every worker instead of
just being skipped.

## Options

| Option | Default | Purpose |
|---|---|---|
| `playbook_repo` | — | SSH URL of the private playbook repo |
| `playbook_branch` | `main` | Branch to track |
| `playbook_subdir` | `ANSIBLE` | Folder within the repo holding the playbooks |
| `update_schedule` | `0 3 * * 0` | Cron for the OS update run |
| `update_playbook` | `cluster-update.yml` | Which playbook that schedule runs |
| `backup_schedule` | `0 2 * * *` | Cron for the datastore backup |
| `backup_enabled` | `true` | Whether to schedule backups at all |
| `sync_before_run` | `true` | Pull the latest playbooks before every run |
| `run_on_start` | `false` | Run the update playbook immediately on start |

## Running on demand

Local add-on containers are named `addon_local_<slug>` for locally installed
add-ons, or `addon_<repo-hash>_<slug>` when installed from a repository —
check `docker ps` for the exact name.

```
docker exec -it <container> /usr/bin/run-ansible-update.sh cluster-update.yml
docker exec -it <container> /usr/bin/run-ansible-update.sh provision-cluster.yml --check
docker exec -it <container> ansible-playbook run-command.yml -e "cmd=uptime"
```

The first form logs to `/share` exactly as the scheduled run does, and
records the repo commit it ran against.

## Logs

- Live: the add-on's **Log** tab, or `ha addons logs <slug>`.
- History: `/share/ansible/logs/<playbook>-<timestamp>.log`, pruned to the
  last 30 per playbook. A non-zero exit is echoed to the add-on log so cron
  can't swallow it. Preview runs log to `<playbook>-preview-<timestamp>.log`
  and share that same 30-log budget.

## Behaviour worth knowing

**Playbooks are pulled before every run, not just at container start.** Cron
runs inside an already-running container, so without this a container that
has been up for weeks would keep executing the commit it cloned at the last
restart. With `sync_before_run` on (the default), a commit and a push is
enough — no add-on restart needed. Each log records the commit it ran
against.

**A failed fetch is not fatal.** If GitHub is unreachable the add-on keeps the
last good checkout and carries on, rather than leaving you with no playbooks.
The log says which happened.

**Permissions are re-applied on every start.** `/share` doesn't reliably
preserve permission bits, and sshd silently ignores a key that is
group-readable — which presents as "permission denied (publickey)" with no
further explanation.

**Nothing secret is in this add-on.** Keys live on `/share`; the inventory and
playbooks live in the private repo. This repository is public.
