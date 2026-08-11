#!/bin/sh
# Runs a playbook from the checked-out private repo, pulling the latest
# commit first so a long-lived container never runs stale playbooks.
#
#   run-ansible-update.sh                        # defaults to cluster-update.yml
#   run-ansible-update.sh provision-cluster.yml
#   run-ansible-update.sh provision-cluster.yml --check   # preview only, changes nothing
set -u

PLAYBOOK="${1:-cluster-update.yml}"
MODE="${2:-}"
LOGDIR=/share/ansible/logs
DIRFILE=/share/ansible/.playbook_dir
ENV_FILE=/share/ansible/.addon_env

SYNC_BEFORE_RUN=true
# shellcheck disable=SC1090
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"

mkdir -p "${LOGDIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ "${MODE}" = "--check" ]; then
    LOG="${LOGDIR}/${PLAYBOOK%.yml}-preview-${STAMP}.log"
else
    LOG="${LOGDIR}/${PLAYBOOK%.yml}-${STAMP}.log"
fi

{
    echo "=== $(date -Iseconds) : ${PLAYBOOK}${MODE:+ (${MODE})} starting ==="

    if [ "${SYNC_BEFORE_RUN}" = "true" ]; then
        /usr/bin/sync-playbooks.sh
    else
        echo "sync: disabled by sync_before_run"
    fi

    if [ ! -f "${DIRFILE}" ]; then
        echo "No playbook directory recorded - did the clone fail?"
        exit 1
    fi

    PLAYBOOK_DIR="$(cat "${DIRFILE}")"
    cd "${PLAYBOOK_DIR}" || exit 1

    if [ ! -f "${PLAYBOOK}" ]; then
        echo "Playbook not found: ${PLAYBOOK_DIR}/${PLAYBOOK}"
        exit 1
    fi

    echo "=== repo commit: $(git -C "${PLAYBOOK_DIR}" rev-parse --short HEAD 2>/dev/null) ==="

    ANSIBLE_CONFIG="${PLAYBOOK_DIR}/ansible.cfg"
    export ANSIBLE_CONFIG
    if [ "${MODE}" = "--check" ]; then
        ansible-playbook "${PLAYBOOK}" --check --diff
    else
        ansible-playbook "${PLAYBOOK}"
    fi
    rc=$?
    echo "=== $(date -Iseconds) : ${PLAYBOOK} finished (exit ${rc}) ==="
    exit ${rc}
} 2>&1 | tee -a "${LOG}" >> /proc/1/fd/1

rc=$?

# Keep the last 30 logs per playbook so /share does not fill up. Preview and
# real-run logs share one counter - both are named ${PLAYBOOK%.yml}-*.log.
ls -1t "${LOGDIR}/${PLAYBOOK%.yml}"-*.log 2>/dev/null | tail -n +31 | xargs -r rm --

# Surface failure in the add-on log rather than letting cron swallow it.
if [ "${rc}" -ne 0 ]; then
    echo "ANSIBLE RUN FAILED: ${PLAYBOOK} exit ${rc} (log: ${LOG})" >> /proc/1/fd/1
fi

exit ${rc}
