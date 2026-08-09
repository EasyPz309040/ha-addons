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

`bootstrap.yml` is deliberately **not** offered as a button. Its first run
against a fresh host is interactive (`--ask-pass`), and it's one-time
provisioning rather than routine operation.

## Options

| Option | Default | Purpose |
|---|---|---|
| `playbook_repo` | — | SSH URL of the private playbook repo |
| `playbook_branch` | `main` | Branch to track |
| `playbook_subdir` | `ANSIBLE` | Folder within the repo holding the playbooks |
| `update_schedule` | `0 3 * * 0` | Cron for the OS update run |
| `update_playbook` | `update.yml` | Which playbook that schedule runs |
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
docker exec -it <container> ansible-playbook run-command.yml -e "cmd=uptime"
```

The first form logs to `/share` exactly as the scheduled run does, and
records the repo commit it ran against.

## Logs

- Live: the add-on's **Log** tab, or `ha addons logs <slug>`.
- History: `/share/ansible/logs/<playbook>-<timestamp>.log`, pruned to the
  last 30 per playbook. A non-zero exit is echoed to the add-on log so cron
  can't swallow it.

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
