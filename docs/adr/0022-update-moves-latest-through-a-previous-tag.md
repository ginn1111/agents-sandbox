# `asb update` keeps one previous build under a `previous` tag and re-installs integrations in a throwaway container

ADR-0012 fixes what `asb update` does. This records how, where ADR-0012 left it open or the first design would have weakened a control. Each rebuilt image (`agent-sandbox-agent`, `agent-sandbox-netguard`) is built with `--no-cache --pull` under a date tag (`2026-11-26`, or `2026-11-26-2` for a second build that day) and only after **both** builds succeed is `latest` moved, so a failed build changes nothing. The build `latest` pointed at becomes `previous`; dated tags that are neither `latest` nor `previous` are removed with a plain `docker rmi` (never `-f`, so an image a running Session still uses stays, and is reported). `asb update --rollback` points `latest` back at `previous` for both images and changes nothing if either lacks one. The CLI upgrade is `uv tool upgrade agent-sandbox` or `pipx upgrade agent-sandbox`, run as an argument list through an injected runner; because the Dockerfiles ship inside the CLI, a full update hands the remaining steps to the freshly installed `asb` (`asb update --after-cli`). The integrations step runs `herdr integration install hermes|omp` in a one-off container from `agent-sandbox-agent:latest` with only that Agent volume mounted, with no network, all capabilities dropped, a read-only root and `no-new-privileges`, so it needs no running Session and never starts one.

## Considered Options

- **Install integrations with `docker exec` in running Sessions**: rejected. Most of the time no Session runs, and it would only reach Agent volumes that a running Session mounts.
- **Install integrations in a throwaway container with the Session's hardening**: chosen. Same restrictions as a Session minus the Workspace and Netguard; it can write only the Agent volume it mounts, which is where the integration files go.
- **Track the previous build with a state file**: rejected. A tag lives with the image, survives a lost file, and `docker image ls` shows it.
- **Move `latest` after each image builds**: rejected. A Netguard build failure would leave a new agent image next to an old Netguard.
- **Keep running `asb update` in the old process after the CLI upgrade**: rejected. It would build the old Dockerfiles, so "latest" CLI changes to the images would not apply until a second `update`.

## Consequences

- Pruning is best effort: an image a running Session uses is reported and retried on the next `update`.
- Rollback goes back one step only, and a second `--rollback` fails. An `update` after a rollback keeps the rolled-back-to build as `previous`.
- The first `update` after a plain `asb build` keeps that untagged `latest` as `previous`, so it can be rolled back to.
- A tag-moving failure is undone in full (`latest` and `previous` restored, the new builds removed).
- The Portal publisher image is not rebuilt: it is pinned Alpine and socat, and ADR-0012 names the agent and Netguard images only.
- Agent volumes are never created, removed or reset by `update`; only the herdr integration files in them are rewritten.
- Unverified without a Docker daemon: see `docs/spikes/update.md`.
