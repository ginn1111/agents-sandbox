# Enable optional capabilities only through explicit commands

Plain `asb up` starts the Session, Netguard (unless offline), and the Watcher, but does not enable any Host Service, Host Browser, or Portal. Configuration defines what is available, not what is active. To enable an optional capability, the user must invoke a separate command that selects it explicitly. That command may start a service inside the Session only if it is required by the selected capability; it must not enable unrelated capabilities. The CLI never starts host programs. This supersedes automatic activation from configuration in ADR-0002, ADR-0008, and ADR-0010; their other restrictions still apply.

## Considered Options

- **Activate configured capabilities on `up`**: convenient, but a config change can open a network path or start a Portal when the user only wanted a Session.
- **Select capabilities with flags on `up`**: explicit, but mixes optional capability activation into Session startup and does not give each operation its own command.

## Consequences

- `asb attach` may still start a plain Session. `asb shell` only enters an existing Session; neither command enables optional capabilities.
- Host Services still require a running host app. The Host Browser still requires Chrome started by the human with a dedicated profile and cannot be enabled by project config.
- Portals still publish on `127.0.0.1` only. The CLI provides explicit start, stop, restart, and status controls for each Portal. For a Portal with a configured background command, lifecycle controls cover both host publication and the process started by the CLI. Without a configured command, lifecycle controls manage only host publication and never start, stop, or restart an independently launched service. `status` distinguishes publication state from service state; publication alone does not prove the service is ready.
- A Portal must be activatable in a running Session without restarting or replacing the Session or its Netguard. If activation fails, report the failure without restarting either container or leaving the Portal published. Docker's creation-time port publication in ADR-0003 and ADR-0010 does not meet this requirement on its own; the live publication mechanism is a separate design decision.
