# Agent login and settings live in one shared Docker volume per agent

Each agent gets one named Docker volume (e.g. `agent-sandbox-claude`), mounted as that agent's home directory in every Session, whatever the Workspace. The agent logs in once inside the sandbox, and every later Session for any Workspace reuses that login. Volumes are per agent, never shared between agents. Nothing is copied from the host's own agent config (`~/.claude`, `~/.codex`), so `import-config` is dropped.

## Considered Options

- **Bind-mount a host folder** (`~/.config/agent-sandbox/agents/<agent>/`): rejected. Volumes keep credentials off the host filesystem tree, avoid UID ownership mismatches on Linux, and behave the same on Colima.
- **One login per Workspace**: rejected. You would have to log in again in every project.
- **`import-config` from the host agent config**: rejected. It would copy hooks, plugins and MCP definitions that can point at host paths or programs.

## Consequences

- Any Session of an agent can read that agent's tokens, and so can any script the agent runs. Accepted: the token belongs to the agent.
- Agent state (history, memory, settings) also lives in the volume, so it carries over from one Workspace to the next.
- Concurrent Sessions of the same agent share the volume. Token refresh conflicts must be tested per agent.
- Settings such as `CLAUDE.md`, skills or MCP config are added by hand inside the sandbox. Backup or reset means `docker volume` commands, e.g. `docker volume rm agent-sandbox-claude` to log out.
