# `asb update` rebuilds everything at latest versions, with one-step rollback

`asb update` (`asb` is installed as a short alias of `agent-sandbox`) does, in order:

1. Upgrade the CLI itself (`uv tool upgrade agent-sandbox`, or the `pipx` equivalent).
2. Rebuild the agent image and the Netguard image with no cache, fetching the **latest** version of every agent, tool and OS package.
3. Re-run `herdr integration install hermes|omp` inside each Agent volume, so integrations match the new herdr.

`--only cli|image|integrations` runs one step. Agent logins and settings are never touched.

Each build is tagged with its date (e.g. `agent-sandbox:2026-11-26`) and `latest` points at it. The previous build is kept, and `asb update --rollback` points `latest` back to it.

Running Sessions keep the image they started with. `update` never stops them; it lists them with "restart to apply: `asb down && asb up`", so herdr panes and agents mid-task aren't killed.

## Considered Options

- **Pinned versions in a versions file**: rejected for now. Reproducible and identical across machines, but every bump is manual. Revisit if teammates hit "works on my machine" differences between builds.
- **Updating running Sessions in place**: rejected. It would kill work in progress.

## Consequences

- Two machines that ran `update` on different days can have different agent versions. A build can't be exactly reproduced.
- Only one previous build is kept, so rollback goes back one step. Older dated images are pruned on the next `update`.
- An agent update that breaks only shows up after `update`; `--rollback` is the recovery path.
