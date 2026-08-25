#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Market Agent starting."

mkdir -p /share/market-agent

# No cron here - the subscriber is a persistent connection started by
# ui.py's own __main__, not a scheduled job, so this process just runs
# in the foreground as the container's only job.
exec env \
  INGRESS_PORT=8099 \
  XWEB_HOST="$(bashio::config 'workflow_service_host')" \
  MARKET_AGENT_SYMBOL="$(bashio::config 'market_agent_symbol')" \
  NOTIFY_SERVICE="$(bashio::config 'notify_service')" \
  SAXO_LOGIN_URL="$(bashio::config 'saxo_login_url')" \
  PRICE_MOVE_THRESHOLD_PERCENT="$(bashio::config 'price_move_threshold_percent')" \
  VOLATILITY_THRESHOLD_PERCENT="$(bashio::config 'volatility_threshold_percent')" \
  SYSTEM_PROMPT="$(bashio::config 'system_prompt')" \
  python3 /usr/bin/ui.py
