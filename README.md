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
| [EasyPz Market Agent](market-agent/) | Live view of xWeb's Market Agent loop — price chart, recent ticks, on-demand real analysis, push notifications on trigger. |

## Why this repo is public and separate

This repo holds only the **add-on shell** — Dockerfile, add-on manifest and
entrypoint. There is nothing environment-specific or secret in it, so making
it public costs nothing and lets Home Assistant install and update the add-on
natively, with no personal access token stored in the Supervisor.

Everything that *is* specific — playbooks, inventory, host addresses — lives
in a private repo, pulled at container start using a read-only deploy key
kept on `/share`. Credentials never enter this repo or the Supervisor's
configuration.
