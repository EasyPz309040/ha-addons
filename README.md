# Home Lab Add-ons

Home Assistant add-on repository for a personal home lab.

Add to Home Assistant: **Settings → Apps → App Store → ⋮ → Repositories**,
then paste:

```
https://github.com/EasyPz309040/ha-addons
```

## Add-ons

| Add-on | What it does |
|---|---|
| [EasyPz Cluster Control](cluster-control/) | Runs Ansible playbooks against a home fleet on a schedule, with the playbooks pulled from a separate private repo before every run. |
| [EasyPz Market Agent](market-agent/) | Live view of the Workflow Service's Market Agent loop — price chart, recent checks, on-demand real analysis, push notifications on trigger. |

## Why this repo is public and separate

This repo holds only the **add-on shell** — Dockerfile, add-on manifest and
entrypoint. There is nothing environment-specific or secret in it, so making
it public costs nothing and lets Home Assistant install and update the add-on
natively, with no personal access token stored in the Supervisor.

The two add-ons keep this true in different ways:

- **`cluster-control`** — everything specific (playbooks, inventory, host
  addresses) lives in a private repo, pulled at container start using a
  read-only deploy key kept on `/share`. Credentials never enter this repo
  or the Supervisor's configuration.
- **`market-agent`** — carries no separate content to protect in the first
  place. It's a thin client: a SignalR subscription and two HTTP calls
  against the Workflow Service's own already-deployed endpoints (a private
  repo, referred to only generically here and in the add-on itself — the
  actual solution name isn't something a user of this add-on needs to
  know). The actual trading logic — trigger thresholds, the Claude system
  prompt, the Claude API key — lives there and is never duplicated or
  shipped here. The one address this add-on hardcodes as a default
  (`workflow_service_host`, a private LAN IP) is documented in its own
  `DOCS.md`, satisfying the "no host addresses beyond what's already in
  the docs" rule below.
