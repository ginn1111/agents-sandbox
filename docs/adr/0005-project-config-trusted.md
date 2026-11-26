# Project configuration is trusted like global configuration

The sandbox never exposes the host outside the Workspace (no home directory, no `~/.ssh`, no host credentials), so the worst a project's `.agent-sandbox.yaml` can do is weaken protection inside that same Workspace. We therefore apply plain precedence (CLI > project > global > defaults) with no trust prompt and no "project may only add masks" rule, to keep the tool simple.

## Considered Options

- **Project config may only add masks**; weakening settings (allow patterns, Host Services, Portals, network mode) only from global config or CLI: rejected as unneeded weight for a tool whose boundary is the Workspace.
- **Per-project trust prompt with a hash store**: rejected for the same reason.

## Consequences

- A committed or agent-edited `.agent-sandbox.yaml` can unmask Workspace secrets (e.g. `allow: ["**/.env"]`) or add Host Services, which opens host ports. `check` must print the config sources it loaded and every rule that came from the project file, so this is visible before each Session.
- This reverses PROPOSE.md's "workspace configuration must not silently weaken mandatory security policies".
