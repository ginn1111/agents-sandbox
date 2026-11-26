# Agent Sandbox: run coding agents confined to one Workspace with Secret files masked

Labels: `ready-for-agent`

## Problem Statement

I run AI coding agents (Hermes and OMP mainly, sometimes Claude Code and Codex) every day on company projects. Today they run directly on my machine with my account's access: they can read my home directory, my SSH keys and cloud credentials, the `.env` files in every project, and anything on the company LAN. One careless command or one bad dependency can leak a secret or touch something outside the project I asked them to work on.

I want to keep using agents the way I do now (several at once, watched in herdr, with Plannotator for plan review, the Hermes Kanban dashboard, and my host apps like DBX and Bruno) but with each agent limited to the one project directory I choose, and with the secret files in that directory hidden from it. It has to be light enough to use all day, and it must not claim protection it doesn't give.

## Solution

A Python CLI, `agent-sandbox` (short alias `asb`), that starts one long-running sandbox container per Workspace. Inside it:

- The Workspace is the only host directory the agent sees, read-write at `/workspace`. Nothing else from the host is mounted: no home, no Docker socket, no SSH agent, no host environment.
- Every Secret file (matched by configurable glob Secret patterns, including through symlinks and hard links) is Masked: it appears empty and can't be written, and the original stays untouched.
- A per-Session Netguard container controls the network: the public internet is open, the host and LAN are blocked except for explicitly listed Host Services (e.g. DBX Web, an LLM gateway), IPv6 is off, and it fails closed.
- herdr TUI runs inside the sandbox as the Watcher. Hermes, OMP and a shell run as herdr panes with herdr's official integrations. I attach to it from my host with `asb attach`.
- Portals (Plannotator and the Hermes dashboard) are published to `127.0.0.1` on a random port, and the URLs are printed.
- Agent logins persist in one Docker volume per agent, shared across Workspaces, so I log in once.
- `asb check` shows exactly what will be masked, skipped and reachable, plus the known limits, without starting anything.

## User Stories

### Starting and using a sandbox

1. As a developer, I want to run `asb up` in a project directory, so that a sandbox starts for that directory without extra arguments.
2. As a developer, I want `asb up ~/work/api` to accept an explicit directory, so that I can start a sandbox for a project I'm not currently in.
3. As a developer, I want `asb up` to do nothing and report the running Session when the sandbox is already up, so that running it twice is safe.
4. As a developer, I want `asb attach` to open the sandbox's herdr in my terminal, so that I can see and drive all agents in that Workspace.
5. As a developer, I want `asb attach` to start the sandbox first if it isn't running, so that one command gets me working.
6. As a developer, I want `asb down` to stop and remove the Session and its Netguard, so that nothing keeps running when I'm done.
7. As a developer, I want `asb down` to keep my Agent volumes, so that I don't have to log in again next time.
8. As a developer, I want `asb ls` to list running Sessions with their Workspace and Portal URLs, so that I can find a sandbox I started earlier.
9. As a developer, I want to run several Workspaces' sandboxes at the same time, so that I can work on several projects in parallel.
10. As a developer, I want concurrent Sessions never to share network, ports or Portals, so that one project's sandbox can't reach another's.
11. As a developer, I want `agent-sandbox` and `asb` to be interchangeable, so that I can type the short one.
12. As a developer, I want the CLI to refuse `/`, my home directory itself, or any path above home as a Workspace, so that I can't expose everything by mistake.
13. As a developer, I want Workspace paths with spaces to work, so that real project names don't break the tool.
14. As a developer, I want the CLI to warn me about external git metadata (a linked worktree, or a `.git` file pointing elsewhere) and not mount it, so that nothing outside the Workspace is exposed implicitly.

### Agents and herdr

15. As a developer, I want Hermes, OMP, Claude Code, Codex and a shell available in every sandbox, so that I can start whichever agent I need as a herdr pane.
16. As a developer, I want herdr's official Hermes and OMP integrations to work inside the sandbox, so that herdr shows their working/idle/blocked state and can resume their sessions.
17. As a developer, I want my host's herdr never to be connected to the sandbox (no socket mount, no relay), so that a sandboxed agent can't run commands on my host through herdr.
18. As a developer, I want agents defined as config entries (a command plus env), so that adding an agent means installing it in the image and adding a config entry, with no code change.
19. As a developer, I want each agent's login and settings in its own Docker volume, shared across Workspaces, so that I log in once per agent.
20. As a developer, I want agent volumes never shared between agents, so that one agent can't read another's tokens.
21. As a developer, I want nothing copied from my host agent config (`~/.claude`, `~/.codex`, `~/.hermes`), so that host hooks, plugins and MCP definitions don't leak into the sandbox.
22. As a developer, I want to reset an agent's login by removing its volume, so that logging out is simple.

### Secret masking

23. As a developer, I want every file matching a Secret pattern to appear empty inside the sandbox, so that the agent can't read its contents.
24. As a developer, I want masked files to be read-only inside the sandbox, so that the agent can't overwrite, delete or rename them.
25. As a developer, I want the original secret files on my host never read, modified or truncated, so that masking is safe.
26. As a developer, I want Secret directories (e.g. `.ssh/`, `secrets/`) masked as empty read-only directories, so that their whole contents are hidden.
27. As a developer, I want a sensible default set of Secret patterns (`.env*`, keys, certificates, `.npmrc`, `.netrc`, cloud credential files, `.git/config`), so that I'm protected without configuring anything.
28. As a developer, I want default allow patterns for `.env.example`, `.env.sample` and `.env.template`, so that harmless templates stay readable.
29. As a developer, I want to add my own Secret patterns in global or project config, so that I can cover project-specific secret files.
30. As a developer, I want a symlink with a secret name to mask the real file it points to inside the Workspace, so that the secret can't be reached through its real path.
31. As a developer, I want every hard link to a masked file also masked, so that the same content can't be read under a harmless name.
32. As a developer, I want discovery never to follow symlinks that point outside the Workspace, so that scanning can't wander into my host.
33. As a developer, I want `.git/config` (and submodule configs) masked by default, so that tokens in remote URLs and auth headers aren't exposed.
34. As a developer, I want local git (status, diff, branch, add, commit, log) to keep working with `.git/config` masked, so that agents can still commit locally.
35. As a developer, I want Skip patterns (default `node_modules/`, `vendor/`, `dist/`, `libs/`) excluded from scanning, so that large dependency and build trees don't slow startup.
36. As a developer, I want `up` to fail if any mask can't be applied, so that a Session never starts with a secret exposed.
37. As a developer, I want `asb attach` to warn me when new files matching Secret patterns appear after `up`, so that I know to restart the sandbox to mask them.

### Checking before running

38. As a developer, I want `asb check` to list every masked path with the reason (pattern, symlink, hard link), so that I can verify masking before starting.
39. As a developer, I want `asb check` to list allowed and skipped paths, so that I can see what is deliberately not masked.
40. As a developer, I want `asb check` to list reachable Host Services and the configured Portals, so that I know what the agent can reach.
41. As a developer, I want `asb check` to list which config files were loaded and every rule that came from project config, so that a committed `.agent-sandbox.yaml` can't silently weaken protection.
42. As a developer, I want `asb check` to state the known limits, so that I don't overestimate the protection.
43. As a developer, I want `asb check` never to start a container, so that it's safe and fast to run anytime.
44. As a developer, I want `asb check --json`, so that scripts and tests can read the exact plan.

### Network

45. As a developer, I want agents to reach the public internet, so that package installs, docs and model APIs work.
46. As a developer, I want the host and LAN (private and link-local ranges) blocked by default, so that agents can't reach company services or my host's ports.
47. As a developer, I want to list Host Services as `host:port` in config, so that agents reach exactly those host apps (e.g. DBX Web on 4224, an LLM gateway).
48. As a developer, I want Host Services to be network-only with no host credentials passed in, so that the host app keeps its own authentication.
49. As a developer, I want DNS to work inside the sandbox even though my resolver is on a private address, so that blocking the LAN doesn't break name resolution.
50. As a developer, I want `host.docker.internal` to work on Linux, Colima and Docker Desktop, so that Host Services are reachable the same way everywhere.
51. As a developer, I want IPv6 disabled in the sandbox, so that the IPv4 rules can't be bypassed over IPv6.
52. As a developer, I want the sandbox to lose network entirely if Netguard can't apply its rules or stops, so that it never falls back to an open network.
53. As a developer, I want the agent container to keep all capabilities dropped while Netguard alone holds `NET_ADMIN`, so that the agent can't change its own firewall.
54. As a developer, I want `--offline` to give the sandbox no network at all, so that I can run fully disconnected work.

### Host Browser

55. As a developer, I want agents to drive a Chrome running on my host through `chrome-devtools-mcp`, so that I can watch the browser window while they test web apps.
56. As a developer, I want the Host Browser off by default and enabled only from global config or a CLI flag, so that a project's config can't turn it on.
57. As a developer, I want `check` and `up` to warn that with the Host Browser enabled the agent can reach my intranet and `file://`, so that I enable it knowingly.
58. As a developer, I want `asb doctor` to print the exact Chrome launch command with a dedicated profile, so that agents never get my everyday cookies and logins.
59. As a developer, I want the sandbox never to start Chrome or any other host program itself, so that it doesn't execute anything on my host.

### Portals

60. As a developer, I want Plannotator inside the sandbox published to `127.0.0.1` on a random host port, so that I can review plans in my browser without exposing it to the network.
61. As a developer, I want the Hermes dashboard (with its Kanban tab) published the same way, so that I can watch Hermes tasks from my browser.
62. As a developer, I want the Portal URLs (and generated logins) printed by the explicit Portal activation command, and the active Portal URLs listed by `ls`, so that I can open them.
63. As a developer, I want the CLI never to open a browser by itself, so that it behaves the same over SSH and in herdr panes.
64. As a developer, I want the Hermes dashboard to require a generated username and password, so that it can listen inside the container safely.
65. As a developer, I want Portal secrets generated once per Workspace and stored outside the Workspace, so that project config and the repo never contain them.
66. As a developer, I want each Workspace's Hermes Kanban dispatcher pinned to that Workspace's board, so that one sandbox doesn't start workers for another project's tasks.
67. As a developer, I want to define more Portals in config (port, optional command, env, path), so that new in-sandbox web tools need no code change.
68. As a developer, I want Portals to be one-way (I reach in, nothing reaches out), so that they never become a path from the sandbox to my host.

### Configuration

69. As a developer, I want a global config at `~/.config/agent-sandbox/config.yaml` and an optional project `.agent-sandbox.yaml`, so that I can set defaults once and override per project.
70. As a developer, I want precedence CLI, then project, then global, then defaults, so that behaviour is predictable.
71. As a developer, I want invalid or unknown config keys rejected with a clear error, so that typos don't silently disable protection.
72. As a developer, I want resource limits (CPUs, memory, PIDs) configurable, so that a runaway agent can't freeze my machine.

### Container hardening

73. As a developer, I want the container's root filesystem read-only with tmpfs for `/tmp` and `/run`, so that the agent can't modify the image at runtime.
74. As a developer, I want the agent to run as a non-root user with no new privileges, so that privilege escalation inside the container is harder.
75. As a developer, I want no host PID/IPC namespaces, devices, Docker socket or SSH agent socket, so that the agent has no path to host control.
76. As a developer, I want no host environment variables passed in, so that tokens in my shell environment never reach the agent.

### Toolchains

77. As a developer, I want Node LTS, Python and Go available through mise in every sandbox, so that common projects work out of the box.
78. As a developer, I want a project's `mise.toml` / `.tool-versions` to install other versions on demand into a shared cache volume, so that per-project toolchains work despite the read-only root.

### Image lifecycle

79. As a developer, I want `asb build` to build the agent and Netguard images, so that I can set up a new machine.
80. As a developer, I want `asb update` to upgrade the CLI, rebuild the images with the latest agents and tools, and re-install the herdr integrations, so that one command keeps everything current.
81. As a developer, I want `asb update --only cli|image|integrations`, so that I can update one part.
82. As a developer, I want `asb update` never to stop running Sessions, only to tell me which need `asb down && asb up`, so that work in progress isn't killed.
83. As a developer, I want each build tagged with its date and the previous one kept, with `asb update --rollback` switching back, so that a broken agent update is easy to undo.
84. As a developer, I want `asb update` never to touch Agent volumes' logins and settings, so that updating doesn't log me out.

### Diagnostics and honesty

85. As a developer, I want `asb doctor` to check Docker/Colima, Python, the images, firewall support and `host.docker.internal`, so that I can fix setup problems quickly.
86. As a developer, I want the README and `check` to state the known limits plainly (filename-only masking, git history, files created after `up`, workspace files that later run on the host, shared kernel), so that nobody treats this as full containment.
87. As a security-minded teammate, I want the tool never to claim protection against a hijacked agent, so that we don't rely on it for that.

## Implementation Decisions

Terms follow `GLOSSARY.md`; reasons and trade-offs are in `docs/adr/0001`–`0012`, and `docs/SPEC.md` holds the full reference (default patterns, example `check` output, example config).

- **Scope (ADR-0001):** confinement to the Workspace and Masking only. Not containment of a hijacked agent, not internet allowlisting, no sanitized-copy mode.
- **Language and runtime (ADR-0011):** Python 3.11+, standard library plus a YAML parser, installed with `uv tool install` / `pipx`, exposing `agent-sandbox` and `asb`. All container work goes through the `docker` CLI as a subprocess with argument lists (never shell strings), using the user's current Docker context.
- **Modules** (deep modules with small interfaces):
  - **Config:** loads defaults, global, project and CLI options; merges by precedence; validates strictly; records the source of each rule.
  - **Discovery:** walks the Workspace and applies Secret, allow and Skip patterns. It resolves symlinks with secret names to their real targets inside the Workspace, finds every hard link to a matched inode, and never follows links outside the Workspace. Returns masked, allowed and skipped entries, each with a reason.
  - **Planner, the main seam:** `plan(workspace, config) → SessionPlan`. A pure function with no Docker calls. SessionPlan contains: the Workspace validation result, mask entries with reasons, the exact argument lists for the Netguard and Session `docker run`, the Netguard rule set, Portal port mappings and generated-secret references, Agent and mise volumes, env, resource limits, warnings and limits text. `asb check` renders it, and `asb check --json` serialises it.
  - **Runtime:** executes a SessionPlan with the `docker` CLI (create the empty mask file and directory sources, start Netguard, wait until it's ready, start the Session and its Watcher). Also handles `down`, `attach`, `shell`, `ls` (by container labels) and failure cleanup. Optional capabilities require separate, explicit activation (ADR-0013).
  - **Netguard:** an Alpine image with `iptables` and a script. Its inputs are allowed Host Service `host:port` pairs, the DNS resolver and the gateway IP. It applies the rules, disables IPv6, signals ready, then idles. The Session joins with `--network container:<netguard>`. Portal ports and `--add-host` are set on Netguard.
  - **Image:** one Dockerfile containing the agents, herdr, Plannotator, `chrome-devtools-mcp`, the DBX MCP server, `bru`, git tooling and mise with global Node LTS, Python and Go.
  - **Lifecycle:** `build`, plus `update` (CLI upgrade, no-cache rebuild, re-installing herdr integrations in Agent volumes, dated tags, `--rollback`).
  - **Doctor:** environment checks, and printing the Host Browser launch command.
- **Session model (ADR-0009):** one long-running container per Workspace (`up` / `attach` / `down`). Agents are herdr panes. The Session mounts the Agent volume of each enabled agent, plus the `agent-sandbox-mise` volume.
- **Masking mechanism (ADR-0004):** a read-only bind of an empty file or directory over each target. Contents are never read. `up` fails if any mask fails.
- **Network (ADR-0002, 0003):** default deny to host, private and link-local ranges; allow listed Host Services and DNS to the configured resolver only; internet open; IPv6 off; fail closed.
- **Config trust (ADR-0005):** project config is trusted like global config, except that it can't enable the Host Browser or set Portal secrets. `check` lists every rule that came from project config.
- **Agent volumes (ADR-0006):** one named volume per agent, shared across Workspaces, never across agents. `import-config` is dropped.
- **Host Browser (ADR-0008):** opt-in from global config or CLI only. The agent connects to a host Chrome debugging port that the user starts with a dedicated profile. The sandbox never starts host programs.
- **Portals (ADR-0010):** generic config entries (`port`, optional `command`, `env`, `open`). Published as `127.0.0.1:<random>:<port>`. `{generated}` secrets are kept per Workspace in the CLI's own state. Built-in entries: Plannotator (`PLANNOTATOR_REMOTE=1`, port 19432) and the Hermes dashboard (`--host 0.0.0.0`, generated basic auth, `HERMES_KANBAN_BOARD=<workspace slug>`). OMP is not wired to Kanban.
- **Update (ADR-0012):** always latest, no pinned versions file; keep one previous build for rollback.
- **Commands:** `up`, `attach`, `shell`, `down`, `ls`, `check`, `doctor`, `build`, `update`, plus the explicit activation commands `service enable|disable`, `portal start|stop|restart|status` and `browser enable|disable` (ADR-0013). Dropped from the original proposal: `<agent>`, `ps`, `stop`, `logs`, `import-config`, `init`, allowlist network mode, sanitized copy.

## Testing Decisions

- **A good test checks external behaviour only:** what the plan says, and what an agent inside a real container can see and reach. It never checks internal helpers, call order or private data structures. Tests should survive refactoring of the internals.
- **Seam 1, the Session plan (primary, agreed with Gin):** most tests call `plan(workspace, config)` (or `asb check --json`) on fixture Workspace directories built in temporary directories, and assert on the resulting SessionPlan. This covers:
  - config precedence, validation errors and source attribution;
  - Secret, allow and Skip matching;
  - symlinks with secret names, symlinks pointing out of the Workspace, hard links and nested secret directories;
  - Workspace path refusal (`/`, home, above home, paths with spaces);
  - the exact `docker run` arguments for hardening flags, mounts, volumes, env and limits;
  - Netguard rule sets for given Host Services;
  - Portal mappings and generated-secret handling;
  - Host Browser gating from project config;
  - warnings and limit text.

  No Docker is needed, so these run fast on any machine.
- **Seam 2, end-to-end against real Docker (agreed with Gin):** `asb up` / `asb down` on Linux Docker and Colima, then probing from inside the Session with `docker exec`. Used only for what the plan can't prove:
  - masked files are empty and can't be written, and the host originals are unchanged;
  - the home directory, parent directories and the Docker socket aren't visible;
  - Workspace edits persist on the host;
  - host and LAN are blocked except Host Services, and the internet and DNS work;
  - IPv6 is off, and killing Netguard leaves no network;
  - Portals answer only on host `127.0.0.1`;
  - concurrent Sessions don't conflict;
  - git works with `.git/config` masked;
  - herdr starts and `attach` works.
- **Spikes before relying on assumptions:**
  - Docker resolves a symlinked mount target to the real file;
  - the Hermes dashboard's Host-header check accepts the Portal's `127.0.0.1:<random>`;
  - `HERMES_KANBAN_BOARD` isolates the dispatcher;
  - herdr integrations report state from inside the container;
  - mise installs into the cache volume with a read-only root;
  - Netguard's DNS and `host-gateway` behaviour on Colima.
- **Prior art:** none. The repository has no code yet. These tests set the pattern.

## Out of Scope

- Protecting against a hijacked or malicious agent: tampering with Workspace files that run later on the host (`Makefile`, `package.json`, `.git/hooks`), or exfiltrating over the open internet.
- Allowlisting internet destinations; a sanitized-copy Workspace mode.
- Content-based secret detection; masking secrets in git history; masking files created after `up` (only a warning on `attach`).
- Connecting the sandbox to the host's herdr (socket mount, relay, or a herdr machine over SSH; the last one is deferred).
- Importing host agent config (`import-config`).
- Pinned or reproducible image versions.
- Windows hosts.
- Opening the browser automatically for Portals.
- OMP reporting to the Hermes Kanban board.

## Further Notes

- Source of truth: `GLOSSARY.md`, `docs/adr/0001`–`0012`, `docs/SPEC.md` in this repository. If this issue and an ADR disagree, the ADR wins.
- Build order: (1) config, discovery, planner and `check`; (2) runtime `up`/`down`/`attach`/`ls` and hardening; (3) Netguard and Host Services, `doctor`; (4) image, Agent volumes, mise, `build`/`update`/rollback; (5) Portals; (6) Host Browser; (7) README and a known-limits page.
- Implement one step at a time with tests. Never weaken a control to work around a compatibility problem; record the trade-off in a new ADR instead.
