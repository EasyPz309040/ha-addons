# Market Agent

Market Agent is a Home Assistant panel that displays live checks from a backend service configured by the operator. It is a client only: domain decisions and any billable analysis remain in the backend.

## Configure the backend

Before starting the add-on, set `workflow_service_host` in its local Home Assistant options to the hostname or address of your own HTTPS backend. This value is intentionally blank by default and is not stored in this repository. Also set `market_agent_symbol` to the instrument or data key you want the panel to display; no private or product-specific value is bundled as a default.

Optional settings let you provide a notification service, a login URL, or backend configuration overrides. Store any environment-specific value only in your own Home Assistant configuration.

## Panel

The panel shows connection state, the most recent backend update, a chart when data is available, current metrics, and recent check history. A manual analysis action is sent to the backend; it does not implement or calculate analysis locally.

SignalR reconnects automatically and requests the latest state after reconnecting. The connection indicator describes the add-on's connection to the backend, not whether the backend's own processing loop is healthy.

## Notifications

Set `notify_service` to a notification service configured in your Home Assistant instance. The add-on uses Home Assistant's Supervisor API proxy; it does not need a long-lived Home Assistant token. Notifications are best-effort and do not stop data updates if delivery fails.

Notifications are not sent between `silence_start` and `silence_end` (default 22:30 to 07:00, Home Assistant's timezone). Notifications raised in that window are dropped, not delivered later; the panel still shows the events. Clear either option to disable silencing.

## Options

| Option | Purpose |
|---|---|
| `workflow_service_host` | Required HTTPS hostname or address for your backend. No default is bundled. |
| `market_agent_symbol` | Required symbol or data key to display. No default is bundled. |
| `notify_service` | Optional Home Assistant notification service name. |
| `silence_start` | Start of the quiet period (`HH:MM`, default `22:30`); no notifications are sent until `silence_end`. |
| `silence_end` | End of the quiet period (`HH:MM`, default `07:00`). |
| `auth_login_url` | Optional login URL override for notifications and the status panel. |
| `price_move_threshold_percent` | Optional backend configuration override; blank leaves backend settings unchanged. |
| `volatility_threshold_percent` | Optional backend configuration override; blank leaves backend settings unchanged. |
| `system_prompt` | Optional backend prompt override; blank leaves backend settings unchanged. |

History is loaded from the backend's stored results as slim summary rows (newest page first, with an "Older checks" link that pages back), so the backend must have its database configured. The chart and a check's detail page fetch that one full result on demand. The add-on keeps only a bounded in-memory view and notification state under `/share/market-agent/`.
