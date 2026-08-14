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
LOCKFILE=/share/ansible/.run.lock
STATUSFILE=/share/ansible/.run.status

SYNC_BEFORE_RUN=true
# shellcheck disable=SC1090
[ -f "${ENV_FILE}" ] && . "${ENV_FILE}"

mkdir -p "${LOGDIR}"

# The one place "at most one run" is actually enforced - cron calls this
# script directly with no lock of its own, so a lock that only ui.py knew
# about only protected button clicks against each other, not a scheduled
# cluster-update.yml overlapping a manual click mid-drain. Holding fd 8 open
# for the rest of the script keeps the lock for the whole run, including the
# ansible-playbook child - flock releases automatically whenever this
# process (and anything that inherited fd 8 from it) exits, so a killed or
# crashed run can't leave a stale lock behind the way a pid-file check would.
#
# `-w 5`, not `-n`: ui.py's own status check takes this same lock briefly
# (non-blocking, to test whether something's running) and releases it
# immediately. `-n` would fail this run outright if a cron trigger landed in
# that microsecond window - a scheduled cluster-update.yml skipped until
# next week over a race, not a real conflict. Five seconds absorbs that
# without meaningfully queuing behind an actual in-progress run, which would
# still lose the race the same as before.
#
# LOCKFILE is only ever the flock target, never written to - status goes in
# the separate STATUSFILE instead, so a second invocation opening LOCKFILE
# (truncating it on open, same as this one did) can't wipe the JSON a
# currently-running invocation just wrote.
exec 8>"${LOCKFILE}"
if ! flock -w 5 8; then
    echo "Another Ansible run is already in progress - skipping this run of ${PLAYBOOK}." >> /proc/1/fd/1
    exit 99
fi
printf '{"pid":%s,"playbook":"%s","check":%s}\n' "$$" "${PLAYBOOK}" \
    "$( [ "${MODE}" = "--check" ] && echo true || echo false )" > "${STATUSFILE}"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ "${MODE}" = "--check" ]; then
    LOG="${LOGDIR}/${PLAYBOOK%.yml}-preview-${STAMP}.log"
else
    LOG="${LOGDIR}/${PLAYBOOK%.yml}-${STAMP}.log"
fi

RCFILE="$(mktemp)"

{
    echo "=== $(date -Iseconds) : ${PLAYBOOK}${MODE:+ (${MODE})} starting ==="

    if [ "${SYNC_BEFORE_RUN}" = "true" ]; then
        /usr/bin/sync-playbooks.sh
    else
        echo "sync: disabled by sync_before_run"
    fi

    if [ ! -f "${DIRFILE}" ]; then
        echo "No playbook directory recorded - did the clone fail?"
        echo 1 > "${RCFILE}"
        exit 1
    fi

    PLAYBOOK_DIR="$(cat "${DIRFILE}")"
    cd "${PLAYBOOK_DIR}" || { echo 1 > "${RCFILE}"; exit 1; }

    if [ ! -f "${PLAYBOOK}" ]; then
        echo "Playbook not found: ${PLAYBOOK_DIR}/${PLAYBOOK}"
        echo 1 > "${RCFILE}"
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
    echo "${rc}" > "${RCFILE}"
    exit ${rc}
# `$?` right after a pipeline is tee's exit status, not the { } group's -
# tee essentially always succeeds, so that always read back as 0 regardless
# of what ansible did. The real code goes through RCFILE instead.
} 2>&1 | tee -a "${LOG}" >> /proc/1/fd/1

rc="$(cat "${RCFILE}")"
rm -f "${RCFILE}"

# Keep the last 30 logs per playbook so /share does not fill up. Preview and
# real-run logs share one counter - both are named ${PLAYBOOK%.yml}-*.log.
ls -1t "${LOGDIR}/${PLAYBOOK%.yml}"-*.log 2>/dev/null | tail -n +31 | xargs -r rm --

# Surface failure in the add-on log rather than letting cron swallow it.
if [ "${rc}" -ne 0 ]; then
    echo "ANSIBLE RUN FAILED: ${PLAYBOOK} exit ${rc} (log: ${LOG})" >> /proc/1/fd/1
fi

exit ${rc}
