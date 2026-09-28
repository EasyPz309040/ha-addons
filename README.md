# Home Assistant Add-ons

This public repository provides two independently configured Home Assistant add-ons. Add the repository through **Settings → Apps → App Store → ⋮ → Repositories**, using this repository's public Git URL.

## Add-ons

| Add-on | Purpose |
|---|---|
| [Cluster Control](cluster-control/) | Runs playbooks from a repository configured by the operator. |
| [Market Agent](market-agent/) | Displays live data from a backend service configured by the operator. |

See each add-on's [Cluster Control documentation](cluster-control/DOCS.md) and [Market Agent documentation](market-agent/DOCS.md) for setup and options.

## Public repository boundary

This repository contains portable add-on software only. It must not contain private IP addresses, personal or private domain names, private repository locations, fleet or device topology, personal contact details, credentials, or other environment-specific operational values. Private endpoints and repositories must be entered by each installer in their own Home Assistant configuration; no real environment values belong in source, examples, diagrams, or defaults.

For Cluster Control, store SSH keys on the Home Assistant host under its `/share` storage and configure only the private repository URL in the local add-on options. For Market Agent, set the backend address and any private market or notification settings in local add-on options. These values must not be committed here.

## Releases and architecture

Each add-on has an independent `slug` and version in its `config.yaml`. Keep slugs stable; bump the version when releasing a change. Base images must use the Supervisor-provided `BUILD_FROM` so the add-on can target its supported architectures.

See [ha-addons-solution.html](ha-addons-solution.html) for the sanitized component overview. It describes software responsibilities only and intentionally omits deployment endpoints and private infrastructure.
