# Home Assistant add-on: EasyPz Market Agent. Live view of xWeb's Market
# Agent loop via a persistent SignalR subscription.
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

# py3-pip + signalrcore: subscribes to xWeb's SignalR hub (/streamHub)
# instead of polling it. The build now needs PyPI reachable too, not just
# Alpine's own mirrors. --break-system-packages: Alpine's system Python
# enforces PEP 668 (externally-managed-environment); there's no competing
# system package to protect here, this is a single-purpose container.
RUN apk add --no-cache python3 py3-pip && \
    pip install --no-cache-dir --break-system-packages signalrcore

LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="addon" \
      io.hass.arch="amd64|aarch64"

COPY run.sh /run.sh
COPY ui.py /usr/bin/ui.py
COPY market_agent.py /usr/bin/market_agent.py
RUN chmod a+x /run.sh /usr/bin/ui.py

CMD [ "/run.sh" ]
