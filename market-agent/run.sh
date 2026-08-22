#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Market Agent starting."

mkdir -p /share/market-agent

# No cron here - the subscriber is a persistent connection started by
# ui.py's own __main__, not a scheduled job, so this process just runs
# in the foreground as the container's only job.
exec env \
  INGRESS_PORT=8099 \
  XWEB_HOST="$(bashio::config 'xweb_host')" \
  MARKET_AGENT_SYMBOL="$(bashio::config 'market_agent_symbol')" \
  NOTIFY_SERVICE="$(bashio::config 'notify_service')" \
  python3 /usr/bin/ui.py
