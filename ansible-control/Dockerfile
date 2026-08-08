# Home Assistant add-on: Ansible control node.
#
# BUILD_FROM is supplied by the Supervisor with the correct architecture base
# image. Hardcoding a base breaks the build on any other arch.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22
FROM $BUILD_FROM

# ansible-core, not the "ansible" metapackage. The playbooks in the private
# repo deliberately use only ansible.builtin modules, so no Galaxy
# collections are needed and the build has no network dependency beyond apk.
RUN apk add --no-cache ansible-core openssh-client git tar

LABEL io.hass.version="0.4.0" \
      io.hass.type="addon" \
      io.hass.arch="amd64|aarch64"

# NOTE: no playbooks are copied in. They are cloned from the private repo at
# container start, so editing a playbook is a commit and a restart rather
# than an image rebuild.
COPY run.sh /run.sh
COPY sync-playbooks.sh /usr/bin/sync-playbooks.sh
COPY run-ansible-update.sh /usr/bin/run-ansible-update.sh
RUN chmod a+x /run.sh /usr/bin/run-ansible-update.sh /usr/bin/sync-playbooks.sh

CMD [ "/run.sh" ]
