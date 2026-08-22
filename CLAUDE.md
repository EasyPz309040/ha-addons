# CLAUDE.md

Home Assistant add-on repository. Contains **add-on shells only** — the
Ansible playbooks `cluster-control` runs live in the private `Home` repo
and are cloned at runtime; `market-agent` has no external content to
clone, it just talks to xWeb over the LAN.

## THIS REPO IS PUBLIC

No keys, no tokens, no inventory, no host addresses beyond what's already in
the docs. Keys live on the HAOS box at `/share/ansible/.ssh/`.

## Two separate add-ons, not one

`cluster-control` (Ansible fleet control) and `market-agent` (live xWeb
view) are two distinct HA sidebar entries, in two distinct folders, each
with its own `slug`, `config.yaml`, and container. They started as one
add-on with a second page bolted on (`ansible-control` → `home-ops`,
2026-08-22) before being split apart the same day — one panel per sidebar
entry turned out to matter more than sharing a container, and the two
concerns have nothing to do with each other. `home-ops` never actually got
installed on HAOS, so the split cost nothing beyond the git history
showing the detour. Don't re-merge them without a real reason; the Market
Agent panel could in principle live inside `cluster-control` again, but
the sidebar UX (one entry per concern) is the reason it doesn't.

## Release mechanics

- `version:` in each add-on's own `config.yaml` **must be bumped** or the
  Supervisor offers no update for that add-on. That's the only place it's
  typed — the Dockerfile's `io.hass.version` reads `ARG BUILD_VERSION`,
  which the Supervisor supplies automatically from `config.yaml` at build
  time.
- `slug:` must **not** change — it's the container name and the ingress
  path. Changing it makes HA treat this as a different add-on and loses
  the install (config values reset, needs uninstall/reinstall). Broken
  deliberately once, 2026-08-22, on the way to the split above — not a
  repeal of the rule.
- `.gitattributes` forces LF. A CRLF shell script dies on Linux with
  `bad interpreter`.

## Constraints

- `ARG BUILD_FROM` — never hardcode a base image; the Supervisor supplies the
  right architecture.
- `cluster-control`: no Galaxy collections. The playbooks use
  `ansible.builtin` only, so the image installs none. Adding one means
  updating the Dockerfile too.
- `market-agent`: `signalrcore` (pip) is its one dependency, for the
  persistent SignalR subscription to xWeb's `/streamHub` — the build needs
  PyPI reachable, not just Alpine's mirrors. Don't add a second dependency
  without the same justification.

See `../Home/CLAUDE.md` for fleet layout and design decisions.
