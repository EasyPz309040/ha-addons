# CLAUDE.md

Home Assistant add-on repository. Contains the **add-on shell only** — the
playbooks it runs live in the private `Home` repo and are cloned at runtime.

## THIS REPO IS PUBLIC

No keys, no tokens, no inventory, no host addresses beyond what's already in
the docs. Keys live on the HAOS box at `/share/ansible/.ssh/`.

## Release mechanics

- `version:` in `ansible-control/config.yaml` **must be bumped** or the
  Supervisor offers no update. That's the only place it's typed — the
  Dockerfile's `io.hass.version` reads `ARG BUILD_VERSION`, which the
  Supervisor supplies automatically from `config.yaml` at build time.
- `slug:` must **not** change — it's the container name and the ingress path.
  Changing it makes HA treat this as a different add-on and loses the install.
- `.gitattributes` forces LF. A CRLF shell script dies on Linux with
  `bad interpreter`.

## Constraints

- `ARG BUILD_FROM` — never hardcode a base image; the Supervisor supplies the
  right architecture.
- No Galaxy collections. The playbooks use `ansible.builtin` only, so the
  image installs none. Adding one means updating the Dockerfile too.

See `../Home/CLAUDE.md` for fleet layout and design decisions.