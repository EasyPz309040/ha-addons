# Home Assistant add-on: Ansible control node.
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

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="amd64|aarch64"

# NOTE: no playbooks are copied in. They are cloned from the private repo at
# container start, so editing a playbook is a commit and a restart rather
# than an image rebuild.
COPY run.sh /run.sh
COPY sync-playbooks.sh /usr/bin/sync-playbooks.sh
COPY ui.py /usr/bin/ui.py
COPY osifont-lgpl3fe.woff /usr/bin/osifont-lgpl3fe.woff
COPY run-ansible-update.sh /usr/bin/run-ansible-update.sh
RUN chmod a+x /run.sh /usr/bin/run-ansible-update.sh /usr/bin/sync-playbooks.sh /usr/bin/ui.py

CMD [ "/run.sh" ]
