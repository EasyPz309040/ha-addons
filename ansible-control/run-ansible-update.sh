#!/bin/sh
# Runs a playbook from the checked-out private repo, logging to both the
# add-on log and a timestamped file under /share.
#
#   run-ansible-update.sh                     # defaults to update.yml
#   run-ansible-update.sh cluster-update.yml
set -u

PLAYBOOK="${1:-update.yml}"
LOGDIR=/share/ansible/logs
DIRFILE=/share/ansible/.playbook_dir

if [ ! -f "${DIRFILE}" ]; then
    echo "No playbook directory recorded - did the git clone fail?" >> /proc/1/fd/1
    exit 1
fi

PLAYBOOK_DIR="$(cat "${DIRFILE}")"
cd "${PLAYBOOK_DIR}" || exit 1

if [ ! -f "${PLAYBOOK}" ]; then
    echo "Playbook not found: ${PLAYBOOK_DIR}/${PLAYBOOK}" >> /proc/1/fd/1
    exit 1
fi

export ANSIBLE_CONFIG="${PLAYBOOK_DIR}/ansible.cfg"

mkdir -p "${LOGDIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="${LOGDIR}/${PLAYBOOK%.yml}-${STAMP}.log"

{
    echo "=== $(date -Iseconds) : ${PLAYBOOK} starting ==="
    echo "=== repo commit: $(git -C "${PLAYBOOK_DIR}" rev-parse --short HEAD 2>/dev/null) ==="
    ansible-playbook "${PLAYBOOK}"
    rc=$?
    echo "=== $(date -Iseconds) : ${PLAYBOOK} finished (exit ${rc}) ==="
    exit ${rc}
} 2>&1 | tee -a "${LOG}" >> /proc/1/fd/1

rc=$?

# Keep the last 30 logs per playbook so /share does not fill up.
ls -1t "${LOGDIR}/${PLAYBOOK%.yml}"-*.log 2>/dev/null | tail -n +31 | xargs -r rm --

# Surface failure in the add-on log rather than letting cron swallow it.
if [ "${rc}" -ne 0 ]; then
    echo "ANSIBLE RUN FAILED: ${PLAYBOOK} exit ${rc} (log: ${LOG})" >> /proc/1/fd/1
fi

exit ${rc}
