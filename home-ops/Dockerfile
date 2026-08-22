# Home Assistant add-on: EasyPz Home Ops. Ansible fleet control plus a
# Market Agent panel fed live from xWeb's SignalR hub.
#
# BUILD_FROM is supplied by the Supervisor with the correct architecture base
# image. Hardcoding a base breaks the build on any other arch.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22
FROM $BUILD_FROM

# The Supervisor passes BUILD_VERSION automatically at build time, sourced
# from this add-on's config.yaml "version:" field - so that field is the
# only place the version needs to be typed. Re-declare the ARG after FROM;
# args declared before FROM don't survive into the build stage.
ARG BUILD_VERSION

# ansible-core, not the "ansible" metapackage. The playbooks in the private
# repo deliberately use only ansible.builtin modules, so no Galaxy
# collections are needed and the build has no network dependency beyond apk.
#
# sshpass: --ask-pass shells out to it for every non-interactive parallel
# SSH connection, even when the prompt itself is interactive - without it
# ansible refuses password auth outright. Needed for provision-cluster.yml's
# documented first-bootstrap invocation (-u pi --ask-pass), before the
# ansible user/key exist on a freshly imaged host.
#
# util-linux: guarantees a real `flock`, which run-ansible-update.sh uses to
# serialize cron and button-click runs against each other.
RUN apk add --no-cache ansible-core openssh-client git tar python3 sshpass util-linux

# py3-pip + signalrcore: the Market Agent panel subscribes to xWeb's
# SignalR hub (/streamHub) instead of polling it. This is the add-on's
# first dependency beyond apk - the build now needs PyPI reachable too,
# not just Alpine's own mirrors. --break-system-packages: Alpine's
# system Python enforces PEP 668 (externally-managed-environment);
# there's no competing system package to protect here, this is a
# single-purpose container.
RUN apk add --no-cache py3-pip && \
    pip install --no-cache-dir --break-system-packages signalrcore

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="amd64|aarch64"

# NOTE: no playbooks are copied in. They are cloned from the private repo at
# container start, so editing a playbook is a commit and a restart rather
# than an image rebuild.
COPY run.sh /run.sh
COPY sync-playbooks.sh /usr/bin/sync-playbooks.sh
COPY ui.py /usr/bin/ui.py
COPY market_agent.py /usr/bin/market_agent.py
COPY osifont-lgpl3fe.woff /usr/bin/osifont-lgpl3fe.woff
COPY run-ansible-update.sh /usr/bin/run-ansible-update.sh
# Read by ui.py's own /docs route (rendered inside the ingress panel) - not
# just relied on via the Supervisor's separate Documentation tab, since that
# tab's own URL scheme has already changed once across HA versions (see
# CLAUDE.md gotchas) and a link to it from inside the panel would be fragile.
COPY DOCS.md /usr/bin/DOCS.md
RUN chmod a+x /run.sh /usr/bin/run-ansible-update.sh /usr/bin/sync-playbooks.sh /usr/bin/ui.py

CMD [ "/run.sh" ]
