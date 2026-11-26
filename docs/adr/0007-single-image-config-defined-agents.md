# One image for all agents; agents are defined in config, not code

All supported agents (Claude Code, Codex, OMP, Hermes) and the shell are installed in a single image, `agent-sandbox`. An agent is only a config entry (`command`, optional `env`): the CLI runs every agent the same way, so adding one means installing it in the image and adding a config entry. There are no per-agent Go adapters. We chose simplicity over image size.

## Considered Options

- **Per-agent images on a shared base**: rejected. Smaller per-agent pulls and independent updates, but more Dockerfiles to maintain.
- **Per-agent Go adapters** (as in PROPOSE.md): rejected. The agents differ only in command and environment, which config covers.

## Consequences

- Language toolchains are managed by mise. The image installs mise and sets global defaults for Node (LTS), Python and Go at build time. A project's `mise.toml` / `.tool-versions` can ask for other versions; mise installs them on demand into a shared cache volume (`agent-sandbox-mise`), because the image's root filesystem is read-only. Other languages are added through mise in the project, not in the image.
- A project's mise config can define tasks and hooks that run code; mise asks for `mise trust` first. Running them inside the sandbox is within the boundary.
- The image is large, and updating any agent means rebuilding it for all agents.
- Every agent's binaries are present in every Session. Since agents now run as herdr panes in one long-running Session (ADR-0009), the Session mounts the Agent volume of each agent enabled for that Workspace, not just one.
