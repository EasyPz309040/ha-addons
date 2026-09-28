# Cluster Control

Cluster Control runs Ansible playbooks from a repository that you configure. The playbooks are fetched at runtime and are not included in this public add-on repository.

## Setup

1. Create an SSH key for the add-on to fetch your private playbook repository. Run these commands in the Home Assistant terminal:

   ```sh
   mkdir -p /share/ansible/.ssh
   ssh-keygen -t ed25519 -f /share/ansible/.ssh/id_deploy -C cluster-control -N ""
   cat /share/ansible/.ssh/id_deploy.pub
   ```

2. Add the public key to your repository as a **read-only deploy key**. Keep the private key on the Home Assistant host under `/share`; never paste it into add-on options or commit it.
3. Configure `playbook_repo` with your repository's SSH clone URL, and set `playbook_branch` and `playbook_subdir` to the appropriate branch and playbook directory. Do not put private repository URLs in public source or documentation.
4. If your managed hosts require SSH host-key verification, add their host keys to `/share/ansible/.ssh/known_hosts` using your own hostnames or addresses. Do not copy private fleet addresses into this documentation.
5. Start the add-on and check its log for synchronization and available playbooks.

The fleet SSH key used by your playbooks is separate from the repository deploy key. Store it in `/share/ansible/.ssh/` and grant only the access your playbooks require.

## Using the panel

The panel lists playbooks found in the configured repository. It serializes runs so scheduled and manually started operations do not overlap. Where a playbook supports check mode, **Preview** runs Ansible with `--check --diff` before any changes are made. Review the plan and log before choosing **Run**.

The first connection to a newly prepared host may require an interactive bootstrap step from a terminal. The add-on panel cannot answer SSH or privilege-escalation prompts; complete that bootstrap using your own playbook documentation before using the panel for that host.

## Scheduling and options

Configure these values in the add-on's local Home Assistant options:

| Option | Purpose |
|---|---|
| `playbook_repo` | Required SSH URL for your private playbook repository. |
| `playbook_branch` | Branch to fetch. |
| `playbook_subdir` | Directory containing playbooks within that checkout. |
| `update_schedule` | Cron schedule for the configured update playbook. |
| `update_playbook` | Playbook run by the update schedule. |
| `backup_schedule` | Cron schedule for the optional backup playbooks. |
| `backup_enabled` | Whether scheduled backups are enabled. |
| `sync_before_run` | Fetch the latest playbooks before each run. Recommended. |
| `run_on_start` | Whether to run the update playbook when the add-on starts. Disabled by default. |

## Running and logs

Use the add-on **Log** tab for live output. Run logs and playbook state are stored under `/share/ansible/`. For manual container operations, first determine the container name on your Home Assistant installation, then run the desired playbook using the add-on's documented entry point.

A successful backup command is not proof of a usable backup. Test restoration using a separate recovery procedure and a safe target before relying on it.

## Operational boundaries

- A failed repository fetch keeps the last successfully fetched checkout; inspect the log to confirm which revision ran.
- Playbooks own their targets, credentials, and operating policy. Review those in your private repository, not in this public add-on.
- This add-on packages Ansible built-ins only. If your playbooks add a Galaxy collection, update the image dependencies and validate the resulting image as well.
