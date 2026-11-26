# Portals are generic config entries; Plannotator and the Hermes dashboard are just two of them

A Portal is declared in configuration: a container port, environment variables and a URL path. The CLI has no special code for any one tool. For each Portal it publishes `127.0.0.1:<random free port>:<container port>` on the host and prints `name → http://127.0.0.1:<port><path>`. It never opens a browser. Portals are one-way: the human reaches in, and nothing in the sandbox reaches the host through them.

```yaml
portals:
  plannotator:
    port: 19432
    env: { PLANNOTATOR_REMOTE: "1", PLANNOTATOR_PORT: "19432" }
    open: /
  hermes-dashboard:
    port: 9119
    command: [hermes, dashboard, --host, 0.0.0.0, --port, "9119", --no-open]
    env:
      HERMES_DASHBOARD_BASIC_AUTH_USERNAME: "{generated}"
      HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: "{generated}"
      HERMES_DASHBOARD_BASIC_AUTH_SECRET: "{generated}"
    open: /
```

- **`command`** (optional) is started in the background inside the container when the Session starts. Without it, the agent starts the service itself (Plannotator is started by the agent's plan hook).
- **`{generated}`** values are random secrets that the CLI creates once per Workspace. It stores them in its own state, outside the Workspace, and prints them with the URL.

## Considered Options

- **Bespoke integration per tool** (as PROPOSE.md had for Plannotator): rejected. Every tool would need its own code, while a port, environment variables and an optional command cover both known cases.
- **Opening the browser automatically**: rejected. Printing the URL is enough and works the same over SSH and in herdr panes.

## Consequences

- The Hermes dashboard must listen on `0.0.0.0` inside the container. On any address other than loopback, Hermes requires a login and refuses to start without one, so its Portal entry has to supply generated basic-auth credentials.
- Unverified, must be tested: Hermes checks that the browser's address matches the address it listens on (DNS-rebinding guard). The browser uses `127.0.0.1:<random>` while the dashboard listens on `0.0.0.0:9119`.
- The agent can read the generated credentials and reach the dashboard. Accepted: the dashboard only controls Hermes inside the same sandbox.
- The Kanban board is for Hermes only. OMP is not wired to it; its status is watched through herdr (ADR-0009).
- `kanban.db` lives in the Hermes Agent volume, which every Workspace shares (ADR-0006). To stop one sandbox's dispatcher starting workers for another Workspace's tasks, the CLI pins each Session to its own board by setting `HERMES_KANBAN_BOARD=<workspace slug>`. Boards persist across Sessions. Whether pinning also stops the dispatcher touching other boards in the same database must be tested.
- Refined by ADR-0020: `HERMES_KANBAN_BOARD` is not relied on to isolate the dispatcher; each Workspace gets its own Kanban home volume instead of sharing `kanban.db` in the Hermes Agent volume.
