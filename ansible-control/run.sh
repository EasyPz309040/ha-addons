#!/usr/bin/with-contenv bashio
set -e

PLAYBOOK_REPO="$(bashio::config 'playbook_repo')"
PLAYBOOK_BRANCH="$(bashio::config 'playbook_branch')"
PLAYBOOK_SUBDIR="$(bashio::config 'playbook_subdir')"
SCHEDULE="$(bashio::config 'update_schedule')"
PLAYBOOK="$(bashio::config 'update_playbook')"
BACKUP_SCHEDULE="$(bashio::config 'backup_schedule')"

SHARE_DIR="/share/ansible"
SSH_DIR="${SHARE_DIR}/.ssh"
FLEET_KEY="${SSH_DIR}/id_ansible"       # authenticates TO the managed hosts
DEPLOY_KEY="${SSH_DIR}/id_deploy"       # read-only key for the private repo
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
REPO_DIR="${SHARE_DIR}/repo"

bashio::log.info "Ansible control node starting."

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

# ---------------------------------------------------------------- playbooks
if [ -n "${PLAYBOOK_REPO}" ]; then
    if [ ! -f "${DEPLOY_KEY}" ]; then
        bashio::log.error "No deploy key at ${DEPLOY_KEY} - cannot fetch playbooks."
        bashio::log.error "  ssh-keygen -t ed25519 -f ${DEPLOY_KEY} -C 'haos-deploy' -N ''"
        bashio::log.error "Then add the .pub to the private repo's Deploy Keys (read-only)."
    fi

    # Seed github.com's host key so the clone cannot hang on a prompt.
    grep -q '^github.com' "${KNOWN_HOSTS}" 2>/dev/null || \
        ssh-keyscan -H github.com >> "${KNOWN_HOSTS}" 2>/dev/null || true

    export GIT_SSH_COMMAND="ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o UserKnownHostsFile=${KNOWN_HOSTS}"

    if [ -d "${REPO_DIR}/.git" ]; then
        bashio::log.info "Updating playbooks from ${PLAYBOOK_BRANCH}."
        git -C "${REPO_DIR}" fetch --prune origin \
            && git -C "${REPO_DIR}" reset --hard "origin/${PLAYBOOK_BRANCH}" \
            || bashio::log.warning "Fetch failed - continuing with the last good checkout."
    else
        bashio::log.info "Cloning playbooks from ${PLAYBOOK_REPO}."
        git clone --branch "${PLAYBOOK_BRANCH}" "${PLAYBOOK_REPO}" "${REPO_DIR}" \
            || bashio::log.error "Clone failed - check the deploy key and repo URL."
    fi
fi

PLAYBOOK_DIR="${REPO_DIR}/${PLAYBOOK_SUBDIR}"
if [ -d "${PLAYBOOK_DIR}" ]; then
    echo "${PLAYBOOK_DIR}" > "${SHARE_DIR}/.playbook_dir"
    bashio::log.info "Playbooks: ${PLAYBOOK_DIR}"
    ls "${PLAYBOOK_DIR}"/*.yml 2>/dev/null | while read -r p; do
        bashio::log.info "  - $(basename "$p")"
    done
else
    bashio::log.error "No playbook directory at ${PLAYBOOK_DIR}."
fi

# ------------------------------------------------------------------- cron
CRONTAB=/etc/crontabs/root
echo "${SCHEDULE} /usr/bin/run-ansible-update.sh ${PLAYBOOK}" > "${CRONTAB}"
bashio::log.info "Update schedule: ${SCHEDULE} (${PLAYBOOK})"

if bashio::config.true 'backup_enabled'; then
    echo "${BACKUP_SCHEDULE} /usr/bin/run-ansible-update.sh backup-datastore.yml" >> "${CRONTAB}"
    bashio::log.info "Backup schedule: ${BACKUP_SCHEDULE} (backup-datastore.yml)"
fi

if bashio::config.true 'run_on_start'; then
    bashio::log.info "run_on_start set - running ${PLAYBOOK} now."
    /usr/bin/run-ansible-update.sh "${PLAYBOOK}" || \
        bashio::log.warning "Startup run failed; the cron schedule is still active."
fi

bashio::log.info "Starting cron. Container stays up for on-demand 'docker exec' runs."
exec crond -f -d 8
