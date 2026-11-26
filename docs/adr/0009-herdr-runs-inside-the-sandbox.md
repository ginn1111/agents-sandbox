# herdr runs inside the sandbox; the human attaches with `docker exec`

Each Workspace's sandbox container runs its own herdr TUI server. Agents (Hermes, OMP, shell) run as panes inside it, so herdr's official integrations (`herdr integration install hermes|omp`, installed in the Agent volumes) talk to herdr's own socket inside the container, unchanged. The human attaches from the host with `agent-sandbox attach <dir>`, which runs `docker exec -it <container> herdr`. The host's herdr is never connected to the sandbox: no socket mount, no relay, no SSH.

## Considered Options

- **herdr on the host only, watching via terminal output** (earlier version of this ADR): rejected. OMP's state only comes from its integration over herdr's socket, so OMP would show no status, and the official integrations couldn't be reused.
- **Mount the host herdr socket into the sandbox**: rejected. Anyone connected to it can run commands as the host user in any pane; that is a host shell.
- **A filtering relay to the host socket** (only `pane.report_agent`, pinned to one pane): rejected. Extra host-side code, and resume commands would need rewriting so herdr didn't restart agents on the host.
- **Add the sandbox to host herdr as a machine over SSH** (`ProxyCommand docker exec … sshd -i`): deferred. It would put sandbox agents in the host sidebar, but needs sshd in the image and generated keys, and non-root `sshd -i` is unverified.

## Consequences

- A sandbox is now a long-running container per Workspace (`up` / `attach` / `down`), not one container per agent run. Agents are herdr panes inside it, and all of them share the Workspace's masks, Netguard and Portals.
- The agent can drive the inner herdr (open panes, send input to other panes). Accepted: that only reaches the same sandbox, where it can already run a shell.
- The inner herdr renders through the human's terminal, so it can set the title, ring the bell and request clipboard writes (OSC 52). Clipboard writes from programs should be disabled in the terminal.
- Sandboxed agents appear in their own herdr window per Workspace, not in the host herdr sidebar.
- The `herdr` binary goes in the image; its version is fixed by the image.
