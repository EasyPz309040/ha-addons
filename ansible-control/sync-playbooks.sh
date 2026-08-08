#!/bin/sh
# Fetch the latest playbooks from the private repo.
#
# Called from two places:
#   - run.sh, at container start
#   - run-ansible-update.sh, before every scheduled or manual run
#
# The second is the important one. Cron runs inside an already-running
# container, so without a pull here a long-lived container would keep
# executing whatever was cloned weeks ago at the last restart.
#
# Config comes from a file written by run.sh, because cron has no bashio
# context.
set -u

ENV_FILE=/share/ansible/.addon_env
[ -f "${ENV_FILE}" ] || { echo "sync: no ${ENV_FILE}; skipping"; exit 0; }
# shellcheck disable=SC1090
. "${ENV_FILE}"

[ -n "${PLAYBOOK_REPO:-}" ] || { echo "sync: no repo configured; skipping"; exit 0; }

SSH_DIR=/share/ansible/.ssh
DEPLOY_KEY="${SSH_DIR}/id_deploy"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
REPO_DIR=/share/ansible/repo

if [ ! -f "${DEPLOY_KEY}" ]; then
    echo "sync: no deploy key at ${DEPLOY_KEY}; using existing checkout"
    exit 0
fi

chmod 600 "${DEPLOY_KEY}" 2>/dev/null || true

grep -q '^github.com' "${KNOWN_HOSTS}" 2>/dev/null || \
    ssh-keyscan -H github.com >> "${KNOWN_HOSTS}" 2>/dev/null || true

GIT_SSH_COMMAND="ssh -i ${DEPLOY_KEY} -o IdentitiesOnly=yes -o UserKnownHostsFile=${KNOWN_HOSTS}"
export GIT_SSH_COMMAND

if [ -d "${REPO_DIR}/.git" ]; then
    if git -C "${REPO_DIR}" fetch --prune origin \
        && git -C "${REPO_DIR}" reset --hard "origin/${PLAYBOOK_BRANCH}"; then
        echo "sync: at $(git -C "${REPO_DIR}" rev-parse --short HEAD)"
    else
        # Deliberately not fatal. A GitHub outage should not stop a
        # scheduled run - the last good checkout is still valid.
        echo "sync: fetch FAILED; continuing with the last good checkout"
    fi
else
    if git clone --branch "${PLAYBOOK_BRANCH}" "${PLAYBOOK_REPO}" "${REPO_DIR}"; then
        echo "sync: cloned at $(git -C "${REPO_DIR}" rev-parse --short HEAD)"
    else
        echo "sync: clone FAILED - check the deploy key and repo URL"
        exit 1
    fi
fi

echo "${REPO_DIR}/${PLAYBOOK_SUBDIR}" > /share/ansible/.playbook_dir
