# Agent Sandbox — Specification

A lightweight CLI, `agent-sandbox` (short alias `asb`), that runs AI coding agents in Docker containers so they can only touch one chosen directory (the Workspace) and can't see the Secret files inside it.

- Terms are defined in [GLOSSARY.md](../GLOSSARY.md). Use them as written.
- Decisions and their reasons are in [docs/adr/](adr/). This spec summarises them; the ADRs win if they disagree.

## 1. Scope

In scope:

- Keep the agent inside the Workspace: no other host files, no host credentials, no Docker socket.
- Mask Secret files in the Workspace.
- Control which host and LAN addresses the agent can reach.

Out of scope (ADR-0001):

- Containing a hijacked or malicious agent. It can still change Workspace files that later run on the host (`Makefile`, `package.json`, `.git/hooks`).
- Allowlisting internet destinations. The public internet stays open.
- A sanitized-copy workspace mode.

The README and `check` output must state these limits plainly. Never claim protection the tool doesn't give.

## 2. Tech stack (ADR-0011)

- **CLI:** Python 3.11+, standard library plus a YAML parser. Installed with `uv tool install` or `pipx`. Ships two commands, `agent-sandbox` and `asb`.
- **Docker access:** the CLI runs the `docker` command as a subprocess, with structured argument lists and never shell strings. It uses the user's current Docker context (Linux Docker, Colima, Docker Desktop).
- **Netguard image:** Alpine with `iptables` and a shell script.
- **Agent image:** one image for all agents (ADR-0007). Toolchains come from mise (§7).
- **Platforms:** Linux and macOS (Colima, Docker Desktop).

## 3. Commands

| Command | What it does |
|---|---|
| `asb up [DIR]` | Resolve the Workspace, load config, discover and mask Secret files, start Netguard (unless offline), then start the Session and its Watcher. Does not enable any Host Service, Host Browser, or Portal. Does nothing if the Session is already running. |
| `asb attach [DIR]` | Run `docker exec -it <ctr> herdr`, after `up` if needed. Warn if Secret pattern matches have appeared since `up`, because those files aren't masked. |
| `asb shell [DIR]` | Open an interactive Fish shell at `/workspace` in the running Session, without attaching to the Watcher. If no Session is running, exit with an error and tell the user to run `asb up`. Do not start or enable a Host Service, Host Browser, or Portal. |
| `asb service enable\|disable <name> [DIR]` | Turn one configured Host Service on or off in the running Session's Netguard, without restarting either container. Activation lasts for the current Session only. |
| `asb portal start\|stop\|restart\|status <name> [DIR]` | Control one configured Portal in the running Session (§8). |
| `asb browser enable\|disable [DIR]` | Turn the Host Browser on or off in the running Session, if global config or the CLI made it available (§6). |
| `asb down [DIR]` | Stop and remove the Session and its Netguard. Agent volumes are kept. |
| `asb ls` | List running Sessions with their Workspace and Portal URLs. |
| `asb check [DIR]` | Dry run: no container starts. See §5 for the output. |
| `asb doctor [DIR]` | Check Docker and Colima, Python, the images, iptables/nft support and `host.docker.internal`. Report whether each configured Host Service answers (from the Session when it is enabled in a running Session, otherwise from the host). Print the Host Browser launch command. |
| `asb build` | Build the agent and Netguard images. |
| `asb update [--only cli\|image\|integrations] [--rollback]` | See ADR-0012. |

When `DIR` is omitted, it defaults to the current directory. Agents aren't CLI commands: you start them as herdr panes inside the Session (ADR-0009).

Host Services, the Host Browser, and Portals require separate, explicit activation commands (ADR-0013). Configuration makes them available for selection; it does not activate them. The CLI never starts host programs.

## 4. Workspace and container

### Workspace
- Resolve the Workspace to a canonical absolute path and mount it at `/workspace`, read-write, as the working directory.
- Refuse `/`, the home directory itself, and any path above home.
- Handle paths with spaces.
- Detect external git metadata (linked worktrees, a `.git` file pointing elsewhere). Warn about it and never mount it automatically.

### Container defaults (both the Session and Netguard, unless noted)
- Read-only root filesystem.
- Non-root user. All capabilities dropped (Netguard keeps only `NET_ADMIN`). `no-new-privileges`. Never `--privileged`.
- No host PID or IPC namespace, no devices, no Docker socket, no SSH agent socket. Default seccomp profile.
- No host environment variables passed through.
- Writable places:
  - `/tmp` and `/run` as tmpfs,
  - Agent volumes mounted at the agents' home directories,
  - the mise cache volume.
- Resource limits are configurable (`cpus`, `memory`, `pids_limit`), and `--shm-size` is not raised.

### Agent volumes (ADR-0006)
- One named volume per agent, shared across Workspaces and never between agents. You log in once inside the sandbox.
- Nothing is imported from host agent config.
- herdr integrations for Hermes and OMP are installed inside these volumes.

## 5. Secret masking (ADR-0004)

- Discovery walks the Workspace and matches **Secret patterns**, with **allow patterns** as exceptions. It skips **Skip patterns**, which default to `node_modules/`, `vendor/`, `dist/` and `libs/`.
- Every match is masked whole:
  - a file is replaced by an empty read-only file,
  - a directory by an empty read-only directory.
- Masking follows aliases:
  - symlinks with a secret name: the real file they point to inside the Workspace is masked;
  - symlinks pointing at a masked file: nothing extra is needed;
  - symlinks pointing outside the Workspace: never followed;
  - hard links: every link to the same inode is masked.
- Contents are never read, and originals are never modified. If a mask can't be applied, `up` fails.

Default Secret patterns:

```text
**/.env  **/.env.*  **/*.pem  **/*.key  **/*.p12  **/*.pfx
**/.npmrc  **/.netrc  **/.git-credentials  **/.ssh/**  **/.aws/**  **/.gnupg/**
**/credentials.json  **/secrets.json  **/secrets.yaml  **/secrets.yml
**/id_rsa  **/id_ed25519  **/.git/config  **/.git/modules/*/config
```

Default allow patterns: `**/.env.example`, `**/.env.sample`, `**/.env.template`.

Example `check` output:

```text
Workspace: /home/user/projects/backend
Config:    ~/.config/agent-sandbox/config.yaml, .agent-sandbox.yaml (project)

MASKED   .env
MASKED   notes.txt            (hard link to .env)
MASKED   .git/config
ALLOWED  .env.example         (allow pattern)
SKIPPED  node_modules/        (skip pattern)

Host Services:  dbx-web host.docker.internal:4224
Portals:        plannotator, hermes-dashboard
Project config: allow "**/.env.local" (weakens masking)
Warnings:       Host Browser enabled: agent controls host Chrome (intranet and file:// reachable)

Limits: masking is by filename only; secrets in git history and files created after `up` are not masked.
Result: Ready
```

## 6. Network

- **Netguard (ADR-0003):** one container per Session that holds `NET_ADMIN`. The Session joins its network with `--network container:<netguard>`. IPv6 is disabled. If Netguard can't apply its rules or stops running, the Session has no network, never an open one.
- **Policy (ADR-0002, ADR-0013):** the host and private, link-local and LAN ranges are blocked except for explicitly activated Host Services and the separately activated Host Browser. The public internet is open.
  - DNS is allowed only to the configured resolver.
  - `--add-host=host.docker.internal:host-gateway` is set on Linux; a hostname mapping alone does not grant access.
- **Host Services:** defined as `host:port` in config. Netguard permits only explicitly activated entries. The agent never gets host credentials through them. Examples: DBX Web (`DBX_WEB_URL`), an LLM gateway.
- **Host Browser (ADR-0008, ADR-0013):** off by default. Settings come only from global config or the CLI; activation requires the explicit CLI command. `check` warns when it is enabled. You start Chrome yourself with a dedicated `--user-data-dir`; `doctor` prints the command. Agents control it with `chrome-devtools-mcp`.
- **`--offline`:** Docker `none` network. No Host Services or Portals.

## 7. Image contents

- Agents: Hermes, OMP, Claude Code, and Codex.
- The initial agent image ships Fish and Starship. `asb shell` uses an image-provided interactive prompt with an `[asb]` badge, the current directory, and Git branch when available (for example, `[asb] backend on main ❯`). It does not import host shell settings or change the shell used to launch agents or run scripts.
- Tools: `git`, `curl`, `bash`, `ripgrep`, `jq`, herdr TUI, Plannotator, `chrome-devtools-mcp`, the DBX MCP server, and `bru`.
- **mise** with global defaults: Node LTS, Python and Go, installed at build time. A project's `mise.toml` can ask for other versions; they install into the `agent-sandbox-mise` volume.

## 8. Portals (ADR-0010)

- A Portal is defined in config as a container port, an optional background `command`, optional env, and a URL path.
- Only explicitly started Portals are published to the host at `127.0.0.1` on a randomly allocated host port. Stopping a Portal releases its host port; the URL may change the next time it starts. `start` prints the current URL and any generated login; `status` shows the current URL while published. The CLI never opens a browser.
- Each Portal supports `start`, `stop`, `restart`, and `status` without restarting the Session or Netguard (ADR-0013). Publication is live, through one publisher container per started Portal (ADR-0018). The CLI manages the service process only if it started that process through the configured `command`; otherwise it manages host publication only. `status` reports publication separately from service state.
- Portal activation belongs to the current Session. `asb down` unpublishes its Portals and releases their host ports. On the next `asb up`, every Portal starts off and must be started explicitly again. Configuration and `{generated}` logins persist across Sessions; the CLI stores generated secrets per Workspace, outside the Workspace.
- Built-in entries:
  - **plannotator**, using `PLANNOTATOR_REMOTE=1` and port 19432;
  - **hermes-dashboard**, using `--host 0.0.0.0` with generated basic-auth credentials (`{generated}`, created once per Workspace, kept in `$XDG_STATE_HOME/agent-sandbox/portal-secrets/`, printed by `portal start`, never by `status`, `ls` or `check`). Its Kanban board is for Hermes only: each Workspace has its own Kanban home volume `agent-sandbox-kanban-<slug>` (`HERMES_KANBAN_HOME=/kanban`) and `HERMES_KANBAN_BOARD` is set to the Workspace slug (ADR-0020).

## 9. Configuration (ADR-0005)

- Global config: `~/.config/agent-sandbox/config.yaml`. Project config: `.agent-sandbox.yaml`.
- Precedence: CLI, then project, then global, then defaults. Project config is trusted, with two exceptions it can't change: the Host Browser and Portal secrets.
- `check` lists every rule that came from project config.
- Invalid or unknown keys are rejected, never ignored.

```yaml
version: 1
resources: { cpus: 4, memory: 4g, pids_limit: 512 }
secrets:
  mask:  ["**/secrets/**"]       # added to defaults
  allow: ["**/.env.example"]
  skip:  ["node_modules/", "vendor/", "dist/", "libs/"]
network:
  host_services:
    - { name: dbx-web,     host: host.docker.internal, port: 4224 }
    - { name: llm-gateway, host: host.docker.internal, port: 20128 }
  host_browser: false             # global config / CLI only
agents:
  hermes: { command: [hermes] }
  omp:    { command: [omp] }
  claude: { command: [claude] }
  codex:  { command: [codex] }
portals: { }                      # see ADR-0010
```

## 10. Git

- Local operations work inside the sandbox: status, diff, branch, add, commit, log.
- `.git/config` is masked by default, so the sandbox has no remotes and no push or pull.
- Host git credentials and SSH keys are never mounted.
- Secrets already in git history stay readable.

## 11. Known limitations (state these in the README and in `check`)

- Masking is by filename. Secrets inside other files aren't detected.
- Secrets committed to git history stay readable.
- Files created after `up`, or under Skip patterns, aren't masked.
- The agent can change Workspace files that later run on the host.
- Host Services can return sensitive data. The Host Browser, when enabled, exposes the host's whole network view and host files.
- Containers share the host kernel.
- Agent credentials in Agent volumes are readable by the agent and by anything it runs.
- The inner herdr can set the terminal title and request clipboard writes (OSC 52). Turn off clipboard writes from programs in your terminal.

## 12. Tests

- **Unit tests (pure Python, no Docker):**
  - config loading, precedence and validation;
  - glob, allow and skip matching;
  - symlink and hard-link discovery;
  - workspace path validation;
  - mount plan and `docker` argument generation;
  - port allocation and Portal secret generation.
- **Integration tests (real Docker on Linux and Colima):**
  - Workspace read and write, with changes persisted;
  - masked files are empty and can't be written, and originals are unchanged;
  - home, parent directories and the Docker socket aren't visible;
  - Netguard blocks host and LAN except Host Services, fails closed, and IPv6 is off;
  - Portals are reachable only on `127.0.0.1`;
  - concurrent Sessions don't conflict;
  - git works with `.git/config` masked.
- **Spikes to verify** (assumptions in the ADRs):
  - Docker follows a symlink mount target to the real file;
  - the Hermes dashboard's Host-header check accepts the Portal;
  - `HERMES_KANBAN_BOARD` isolates the dispatcher;
  - herdr integrations work inside the container;
  - mise installs into the cache volume with a read-only root filesystem.

## 13. Build order

1. Core: config, discovery and masking, `check`, the `up`/`down`/`attach`/`ls` lifecycle, container defaults.
2. Netguard and Host Services, `doctor`.
3. The image (agents, herdr, mise toolchains), Agent volumes, `build`, `update` and rollback.
4. Portals: Plannotator and the Hermes dashboard.
5. Host Browser.
6. README, a known-limits page and install instructions.

Implement one step at a time. Run tests after each step. Never weaken a control to work around a compatibility problem; record the trade-off in an ADR instead.
