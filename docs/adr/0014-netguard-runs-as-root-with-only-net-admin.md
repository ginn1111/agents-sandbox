# Netguard runs as root inside its container, holding only NET_ADMIN

SPEC §4 asks for a non-root user in every container. Netguard can't meet that: Docker gives `--cap-add NET_ADMIN` to the container's bounding set, but a non-root process starts with no effective capabilities (Docker sets no ambient capabilities), and `no-new-privileges` stops file capabilities on `iptables` from granting them. So `iptables-restore` fails as non-root. Netguard therefore runs as uid 0, with `--cap-drop ALL --cap-add NET_ADMIN`, `no-new-privileges`, a read-only root filesystem, a tmpfs only at `/run`, no mounts, no published ports, no host environment and a 64 MB / 64 PID limit. Without `CAP_DAC_OVERRIDE`, `CAP_CHOWN` and the rest, root here can do little beyond the network changes it needs. The Session, the container that runs the agent, stays non-root with every capability dropped and cannot reach Netguard's process or filesystem (separate PID and mount namespaces; only the network is shared).

## Considered Options

- **Non-root Netguard with file capabilities on `iptables`**: rejected. `no-new-privileges` (a fixed control) blocks it, and dropping `no-new-privileges` would weaken the control for a convenience.
- **Non-root Netguard with ambient capabilities**: rejected. Docker has no flag for ambient capabilities, so it would need a launcher that holds root first.
- **Rules applied by the host or the CLI with `docker exec --privileged`**: rejected. That's the same problem as ADR-0003's "rules on the host".

## Consequences

- A bug in the Netguard script runs as uid 0 inside the container, with only `NET_ADMIN`. The script is small and has no input from the Workspace or the agent; its only input is the rule set the CLI generates.
- `NET_ADMIN` lets Netguard change the shared network namespace, which is the point. The Session can't, because it holds no capabilities.
- Container escape through a Netguard kernel bug is the same class of risk as for the Session ("containers share the host kernel", SPEC §11).
