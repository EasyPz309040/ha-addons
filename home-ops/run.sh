#!/usr/bin/with-contenv bashio
set -e

SHARE_DIR="/share/ansible"
SSH_DIR="${SHARE_DIR}/.ssh"
FLEET_KEY="${SSH_DIR}/id_ansible"
DEPLOY_KEY="${SSH_DIR}/id_deploy"

bashio::log.info "Home Ops starting."

mkdir -p "${SHARE_DIR}/logs" "${SHARE_DIR}/backups" "${SSH_DIR}"

# /share does not always preserve permission bits, and sshd silently ignores
# a key that is group- or world-readable. Re-apply on every start.
chmod 700 "${SSH_DIR}" 2>/dev/null || true
[ -f "${FLEET_KEY}" ]  && chmod 600 "${FLEET_KEY}"
[ -f "${DEPLOY_KEY}" ] && chmod 600 "${DEPLOY_KEY}"

if [ ! -f "${FLEET_KEY}" ]; then
    bashio::log.error "No fleet SSH key at ${FLEET_KEY}."
    bashio::log.error "  ssh-keygen -t ed25519 -f ${FLEET_KEY} -C 'ansible@haos' -N ''"
fi
if [ ! -f "${DEPLOY_KEY}" ]; then
    bashio::log.error "No deploy key at ${DEPLOY_KEY} - cannot fetch playbooks."
    bashio::log.error "  ssh-keygen -t ed25519 -f ${DEPLOY_KEY} -C 'haos-deploy' -N ''"
    bashio::log.error "Then add the .pub to the private repo's Deploy Keys (read-only)."
fi

# Cron has no bashio context, so hand the config to sync-playbooks.sh via a
# file. This is what lets every scheduled run pull before it executes.
cat > "${SHARE_DIR}/.addon_env" <<ENVEOF
PLAYBOOK_REPO='$(bashio::config 'playbook_repo')'
PLAYBOOK_BRANCH='$(bashio::config 'playbook_branch')'
PLAYBOOK_SUBDIR='$(bashio::config 'playbook_subdir')'
SYNC_BEFORE_RUN='$(bashio::config 'sync_before_run')'
ENVEOF

bashio::log.info "Syncing playbooks."
/usr/bin/sync-playbooks.sh || bashio::log.warning "Initial sync reported a problem."

PLAYBOOK_DIR="$(cat "${SHARE_DIR}/.playbook_dir" 2>/dev/null || true)"
if [ -n "${PLAYBOOK_DIR}" ] && [ -d "${PLAYBOOK_DIR}" ]; then
    bashio::log.info "Playbooks: ${PLAYBOOK_DIR}"
    ls "${PLAYBOOK_DIR}"/*.yml 2>/dev/null | while read -r p; do
        bashio::log.info "  - $(basename "$p")"
    done
else
    bashio::log.error "No playbook directory found."
fi

SCHEDULE="$(bashio::config 'update_schedule')"
PLAYBOOK="$(bashio::config 'update_playbook')"
BACKUP_SCHEDULE="$(bashio::config 'backup_schedule')"

CRONTAB=/etc/crontabs/root
echo "${SCHEDULE} /usr/bin/run-ansible-update.sh ${PLAYBOOK}" > "${CRONTAB}"
bashio::log.info "Update schedule: ${SCHEDULE} (${PLAYBOOK})"

if bashio::config.true 'backup_enabled'; then
    # ';' not '&&' - the datastore backup failing shouldn't skip the
    # secrets backup, they're independent. Sequential on one cron line
    # rather than two separate entries so they never run concurrently
    # against the same control plane (nothing else enforces that for
    # cron-triggered runs - only the web UI's own button click does).
    echo "${BACKUP_SCHEDULE} /usr/bin/run-ansible-update.sh backup-datastore.yml; /usr/bin/run-ansible-update.sh backup-secrets.yml" >> "${CRONTAB}"
    bashio::log.info "Backup schedule: ${BACKUP_SCHEDULE} (backup-datastore.yml, then backup-secrets.yml)"
fi

if bashio::config.true 'sync_before_run'; then
    bashio::log.info "Playbooks are re-synced before every run; a push is enough."
else
    bashio::log.warning "sync_before_run is off - scheduled runs use the checkout from container start."
fi

if bashio::config.true 'run_on_start'; then
    bashio::log.info "run_on_start set - running ${PLAYBOOK} now."
    /usr/bin/run-ansible-update.sh "${PLAYBOOK}" || \
        bashio::log.warning "Startup run failed; the cron schedule is still active."
fi

bashio::log.info "Starting web UI on ingress port 8099."
# XWEB_HOST/MARKET_AGENT_SYMBOL/NOTIFY_SERVICE feed market_agent.py's
# background subscriber - it's spawned in-process by ui.py's __main__,
# not as a separate script here, so it just inherits this shell's
# environment. SUPERVISOR_TOKEN needs no export: the Supervisor already
# injects it into every add-on's environment automatically.
INGRESS_PORT=8099 \
XWEB_HOST="$(bashio::config 'xweb_host')" \
MARKET_AGENT_SYMBOL="$(bashio::config 'market_agent_symbol')" \
NOTIFY_SERVICE="$(bashio::config 'notify_service')" \
python3 /usr/bin/ui.py &

bashio::log.info "Starting cron."
exec crond -f -d 8
