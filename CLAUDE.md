# Home Assistant Add-ons Agent Guide

## Solution Summary

This is the public repository for Home Assistant add-on shells. It packages two distinct features: `cluster-control`, which runs operator-configured playbooks from an external private repository, and `market-agent`, which consumes a separately operated backend over SignalR and HTTP. This repository does not own the private playbooks, cluster manifests, or backend analysis logic.

## Agent Requirements

- Keep this file concise and durable. Put operational procedures in [README.md](README.md) or the relevant add-on documentation, and remove historical incident narratives.
- In agent/auto mode, do not silently choose among materially different approaches. Use the prompt and relevant repository guidance to frame options, recommend one, and ask a focused confirmation question when behavior, security, data, or ownership would change. Routine settled details need no confirmation.
- When work crosses ownership boundaries, coordinate with the private playbook owner, cluster deployment owner, and backend service owner. Consult their current guidance in the local multi-repository workspace; do not expose private repository names or paths here.
- Keep [README.md](README.md) and [ha-addons-solution.html](ha-addons-solution.html) synchronized when changes affect add-on architecture, data flow, release, or deployment; update both in the same change.
- This repository is public. Never commit private or security-compromising information: non-public IP addresses, personal or private domain names, private repository names or URLs, fleet/device topology, usernames or contact details, credentials, tokens, private keys, or environment-specific operational values. Do not expose them in source, defaults, docs, logs, diagrams, metadata, generated artifacts, or examples. Use neutral placeholders and require each installer to configure their own private endpoints and settings. Treat anything already published in Git history as exposed; rotate or replace sensitive credentials and endpoints as appropriate.
- Conserve GitHub Actions minutes to avoid exceeding the monthly allowance: prefer focused local checks, avoid unnecessary workflow runs or reruns, and use CI when required or explicitly requested.

### Solution Layout

```text
ha-addons/
  cluster-control/       public shell for operator-configured private playbooks
  market-agent/          thin configured SignalR/HTTP backend client
  README.md              installation and repository overview
  ha-addons-solution.html architecture and data-flow overview
```

## Guardrails

- **Keep `market-agent` free of proprietary decision logic.** It displays and forwards data from xWeb; trigger thresholds, prompts, Claude calls, and API credentials belong in xWeb. Pydantic models may validate payload shape and types, but must not independently calculate decisions.
- **Keep `cluster-control` as a shell around private playbooks.** Inventory and host-specific operational data remain in Home and are fetched at runtime using the read-only deploy key stored on the HA host, never in this repository or add-on configuration.
- Keep the add-ons separate: each has its own directory, `slug`, configuration, and release version.
- Bump an add-on's `version` in its `config.yaml` when releasing changes; keep its `slug` stable because Home Assistant uses it as the add-on identity.
- Use Supervisor-provided `BUILD_FROM` for architecture-specific base images. Keep shell scripts LF-formatted.
- Keep dependencies justified and pinned. Adding an Ansible Galaxy collection requires corresponding image installation changes; adding a Python package requires PyPI access during image build.

## Solution Concepts

- Each add-on is an independent Home Assistant feature with its own identity, configuration, and release lifecycle.
- `cluster-control` is a generic adapter that fetches operator-configured playbooks and runs them; private inventory, targets, and operational policy remain outside this public repository.
- `market-agent` is a client for an operator-configured backend. It may validate message shape and present backend results, but domain decisions, credentials, and backend configuration remain outside this repository.
- Add-on configuration contains portable defaults only. Private endpoints and environment-specific values must be supplied by the installer in Home Assistant configuration.

## Key Learnings / Gotchas

- `cluster-control` synchronizes playbooks before runs; the private-repository credentials belong on the HA host, not in this public image or its Supervisor options.
- `market-agent` validates xWeb hub payloads because SignalR payloads are not described by OpenAPI. Keep those shape models aligned with xWeb's server definitions while leaving all decisions in xWeb.
- Do not leave pip dependencies unpinned. A floating package version can silently change or freeze behavior independently of this repository's release.
- Preserve LF line endings; CRLF shell scripts fail when run in Linux containers.
