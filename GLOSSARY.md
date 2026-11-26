# Agent Sandbox

A lightweight CLI that runs AI coding agents in containers so they can only touch one chosen directory and can't see the secret files inside it.

## Language

**Workspace**:
The single host directory an agent may read and write during a session. Nothing else on the host is visible to the agent.
_Avoid_: Project dir, repo, mount

**Secret file**:
A file or directory inside the Workspace that is reachable through any path matching a Secret pattern, whether that path is its own name, a symlink to it, or a hard link to it. Identified by path only, never by content.
_Avoid_: Credential, sensitive file, key

**Secret pattern**:
A glob in configuration (global or project) that marks matching paths as Secret files, e.g. `**/.env`, `**/.git/config`. Allow patterns exempt specific paths such as `**/.env.example`.
_Avoid_: Mask rule, secret filter, ignore pattern

**Skip pattern**:
A glob in configuration naming directories that discovery does not scan for Secret files, e.g. dependency or build output such as `dist/`. Nothing under a skipped directory is masked.
_Avoid_: Exclude, ignore list, allow pattern

**Mask**:
To hide a Secret file from the agent: it appears empty and can't be written, while the original on the host stays untouched.
_Avoid_: Redact, hide, block, filter

## Crossing the boundary

**Portal**:
A service running inside the sandbox that the agent uses directly and the human reaches from the host through a localhost-only port. Traffic starts on the host and goes in. Examples: Plannotator, the Hermes dashboard.
_Avoid_: Integration, plugin, port forward

**Host Service**:
An app running on the host that the agent may reach over the network, one listed service at a time. Traffic starts in the sandbox and goes out; the agent never runs host programs or reads host files through it. Examples: DBX Web, an LLM gateway.
_Avoid_: Host tool, host integration, bridge

**Watcher**:
The tool the human uses to watch agents working and see which one needs attention: herdr TUI, running inside the Session and attached to from the host. It never connects to the host's own herdr, and no herdr socket is mounted or relayed across the boundary, because whoever reaches a herdr socket can run commands in its panes.
_Avoid_: Workspace manager (clashes with Workspace), herdr integration, orchestrator

**Host Browser**:
A Chrome on the host, started by the human with a dedicated profile and remote debugging, that the agent controls from the sandbox. Unlike a Host Service, it lets the agent reach whatever the host can, including the intranet and host files, so it is off by default and enabled only from global config or the CLI.
_Avoid_: Host Service (for Chrome), remote browser, browser MCP

## Runtime

**Session**:
One long-running sandbox container for one Workspace, from `up` to `down`. Agents run as herdr panes inside it, sharing its masks, Netguard and Portals. Sessions share nothing with each other at runtime: not network, ports or Portals.
_Avoid_: Run, instance, sandbox (when a single container is meant)

**Netguard**:
The per-Session companion container that owns the Session's network and enforces which hosts the agent may reach.
_Avoid_: Firewall, proxy, gateway, sidecar

**Agent volume**:
The one persistent Docker volume per agent that holds its login and settings, mounted as the agent's home in every Session regardless of Workspace.
_Avoid_: Agent config dir, profile, auth store
