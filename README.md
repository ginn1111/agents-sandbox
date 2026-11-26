# Agent Sandbox

`agent-sandbox` (short alias `asb`) runs AI coding agents in Docker so they can only touch one directory, the **Workspace**, and can't read the **Secret files** inside it. Agents (Hermes, OMP, Claude Code, Codex) run as panes of a herdr TUI inside one long-running container per Workspace, the **Session**. You attach to it from your terminal.

What it does:

- Mounts only the Workspace, read-write, at `/workspace`. No home directory, no Docker socket, no SSH agent, no host environment variables, no host agent config.
- **Masks** Secret files: each one appears empty and read-only inside the Session. The originals on the host are never read or modified.
- Puts the Session behind **Netguard**, a companion container that blocks the host and private, link-local and LAN ranges. You open individual **Host Services** by hand. The public internet stays open.

What it does not do is listed under [Known limits](#known-limits), and `asb check` prints the same list before every Session. In short: this tool does not contain a hijacked or malicious agent, and it does not stop the agent sending Workspace data to the public internet.

> **Status.** The design is implemented and tested without Docker (Seam 1). The behaviour that needs a real Docker daemon is documented from design: the Seam 2 end-to-end tests have not yet been run against a real daemon. See [Known limits](#known-limits).

## Install

Requirements: Python 3.11+, Docker reachable through the `docker` CLI (the CLI uses your current Docker context), and `uv` or `pipx`.

From a checkout of this repository:

```bash
git clone git@github.com:ginn1111/agents-sandbox.git
cd agents-sandbox
uv tool install .        # or: pipx install .
asb --help
```

Both `agent-sandbox` and `asb` are installed and run the same code.

### Linux

Install Docker Engine and make sure your user can run `docker ps`. Nothing else is needed. The CLI adds `--add-host=host.docker.internal:host-gateway` to Netguard so Host Services can be named `host.docker.internal`.

### macOS with Colima

```bash
brew install colima docker uv
colima start
docker context use colima     # or leave the context `colima start` selected
```

Docker Desktop also works through its Docker context. The `docker` CLI is the only interface used; no Docker SDK and no socket path is configured.

### First run

```bash
asb build            # builds the agent, Netguard and Portal publisher images (slow the first time)
asb doctor           # Python, Docker daemon, the three images, each Host Service, the Host Browser command
asb check ~/src/app  # dry run: what is masked, what network, which limits apply
asb up ~/src/app     # start the Session (does nothing if it is already running)
asb attach ~/src/app # open herdr in the Session
```

Inside herdr, open panes and start `hermes`, `omp`, `claude` or `codex`. Log in once inside the sandbox: each agent has its own Agent volume (`agent-sandbox-<agent>`) that keeps its login across Sessions and Workspaces. Nothing is imported from your host agent config.

When `DIR` is omitted, every command uses the current directory. The Workspace must not be `/`, your home directory, or a path above it.

## Commands

`asb --help` and `asb <command> --help` are the reference; this table follows them.

| Command | What it does |
|---|---|
| `asb up [DIR]` | Resolve the Workspace, load config, discover and mask Secret files, start Netguard (unless `--offline`), start the Session. Enables no Host Service, Host Browser or Portal. |
| `asb attach [DIR]` | Run `herdr` in the Session (`docker exec -it`), starting the Session first if needed. On an already-running Session it warns about Secret file matches that have appeared since `up`: those are not masked. |
| `asb down [DIR]` | Stop and remove the Session, its Netguard and its published Portals. Agent volumes and the Kanban volume are kept. |
| `asb check [DIR]` | Dry run, no container starts. Prints the plan; `--json` prints it as JSON. Exit code 0 only when the result is `Ready`. |
| `asb ls` | List running Sessions with their Workspace and the URLs of started Portals. |
| `asb doctor [DIR]` | Check Python, the Docker daemon, the images, and whether each configured Host Service answers. Prints the Host Browser launch command. |
| `asb build` | Build the agent image, the Netguard image and the Portal publisher image. |
| `asb update [--only cli\|image\|integrations] [--rollback]` | See [Update and rollback](#update-and-rollback). |
| `asb service enable\|disable NAME [DIR]` | Open or close one configured Host Service in the running Session. |
| `asb portal start\|stop\|restart\|status NAME [DIR]` | Control one configured Portal in the running Session. |
| `asb browser enable\|disable [DIR]` | Open or close the Host Browser path in the running Session. |

Flags:

- `--offline` (`up`, `attach`, `check`, `browser`): the Session gets Docker's `none` network. No Host Services, no Portals. Pass it to `browser` too when the Session was started with it.
- `--json` (`check`): the plan as JSON.
- `--host-browser` (`check`, `up`, `attach`, `doctor`, `browser`): make the Host Browser available for this command. See [Host Browser](#host-browser).
- `--only` and `--rollback` (`update`).

Optional capabilities are never switched on by `up` or by configuration. Configuration only makes a Host Service, the Host Browser or a Portal *available*; a separate command turns each one on, for the current Session only (ADR-0013).

Git inside the Session: status, diff, branch, add, commit and log work. `.git/config` is masked by default, so the Session has no remotes and no push or pull, and host git credentials and SSH keys are never mounted. If `.git` is a file pointing outside the Workspace (a linked worktree), `check` warns and the external git directory is not mounted, so git will not work in the Session.

## Config reference

Two optional files: global `~/.config/agent-sandbox/config.yaml` (or under `$XDG_CONFIG_HOME`) and project `.agent-sandbox.yaml` in the Workspace root. Precedence: command-line flags, then the project file, then the global file, then defaults. Unknown or invalid keys are an error, never ignored. Every file needs `version: 1`.

Project config is trusted like global config, with two exceptions: project config cannot enable the Host Browser (`network.host_browser`) and cannot set Portal secrets (`portals.<name>.secrets`, and a `login` whose variables are not `{generated}`). `asb check` lists every rule that came from the project file and marks `secrets.allow` and `secrets.skip` rules as weakening masking, because a committed or agent-edited project file can unmask Secret files or open host ports.

```yaml
version: 1
resources: { cpus: 4, memory: 4g, pids_limit: 512 }
secrets:
  mask: ["**/secrets/**"]        # added to the default Secret patterns
  allow: ["**/.env.local"]       # added to the default allow patterns
  skip: ["build/"]               # added to the default Skip patterns
network:
  host_services:
    - { name: dbx-web, host: host.docker.internal, port: 4224 }
  host_browser: false            # global config only
agents:
  hermes: { command: [hermes], env: { SOME_VAR: value } }
portals:
  docs:
    port: 8080
    command: [python3, -m, http.server, "8080"]
    open: /
```

| Key | Meaning |
|---|---|
| `version` | Must be `1`. Required in every config file. |
| `resources.cpus` | CPU limit for the Session. Positive number. Default: `cpus: 4`. |
| `resources.memory` | Memory limit such as `4g`, `512m` or a byte count. Default: `memory: 4g`. |
| `resources.pids_limit` | Process limit. Positive number. Default: `pids_limit: 512`. |
| `secrets.mask` | Extra Secret patterns, added to the defaults. |
| `secrets.allow` | Extra allow patterns, exempting paths from masking. Added to the defaults. |
| `secrets.skip` | Extra Skip patterns: directories discovery does not scan. Added to the defaults. |
| `network.host_services` | List of `{name, host, port}`. `name` is lowercase letters, digits and dashes. `host` is a DNS name or IPv4 address; loopback, `localhost`, link-local, multicast and reserved addresses are refused. Not usable: the name `host-browser` and the Host Browser's address. |
| `network.host_browser` | `true` makes the Host Browser available. Global config only; project config cannot set it. |
| `agents.<name>.command` | The command herdr's pane runs for this agent. Replaces the built-in entry of the same name as a whole. |
| `agents.<name>.env` | Environment for that agent only. `HOME` is set by the Session and cannot be overridden. |
| `portals.<name>.port` | Container port of the Portal. Required. |
| `portals.<name>.command` | Optional background command `asb portal start` runs in the Session. Without it, the agent starts the service itself. |
| `portals.<name>.env` | Environment for the Portal. A value of exactly `{generated}` is a random secret created once per Workspace. |
| `portals.<name>.open` | URL path to print after the address. Must start with `/`. |
| `portals.<name>.login` | `{username: VAR, password: VAR}`: which `env` variables `portal start` shows as the login. |

A config entry for a Portal or agent with a built-in name replaces the built-in as a whole.

### Default Secret patterns

Patterns are gitignore-style, relative to the Workspace, matched on names only and case-sensitively. A pattern with no inner `/` matches at any depth. `secrets.mask` adds to this list; it cannot remove from it.

- `**/.env`, `**/.env.*`, `**/*.pem`, `**/*.key`, `**/*.p12`, `**/*.pfx`
- `**/.npmrc`, `**/.netrc`, `**/.git-credentials`
- `**/.ssh/**`, `**/.aws/**`, `**/.gnupg/**`
- `**/credentials.json`, `**/secrets.json`, `**/secrets.yaml`, `**/secrets.yml`
- `**/id_rsa`, `**/id_ed25519`
- `**/.git/config`, `**/.git/modules/*/config`

### Default allow patterns

Exceptions to the Secret patterns: `**/.env.example`, `**/.env.sample`, `**/.env.template`.

### Default Skip patterns

Directories discovery does not scan, so nothing under them is masked: `node_modules/`, `vendor/`, `dist/`, `libs/`. `.git/` is not a Skip pattern.

### How masking works

Every match is masked whole: a file is replaced by an empty read-only file, a directory by an empty read-only directory. Symlinks with a secret name mask the real file they point to inside the Workspace; symlinks pointing outside the Workspace are never followed; every hard link to a masked file is masked too. If a mask cannot be applied, `asb up` fails. File contents are never read.

### Default agents and Portals

Agents (`command` is the command name): `hermes`, `omp`, `claude`, `codex`. The image also has `git`, `curl`, `bash`, `ripgrep`, `jq`, herdr, Plannotator, `chrome-devtools-mcp`, the DBX MCP server and `bru`. Toolchains come from mise: Node LTS, Python and Go are installed at build time, and a project's `mise.toml` can ask for other versions, which install into the `agent-sandbox-mise` volume.

Portals: `plannotator` (port 19432) and `hermes-dashboard` (port 9119).

## Network, Host Services and Portals

### Netguard

Each Session gets its own Netguard container, which holds the only `NET_ADMIN` capability. The Session joins its network namespace. The host and private, link-local and LAN ranges are blocked, DNS goes only to the configured resolver, and IPv6 is off. If Netguard cannot apply its rules or stops, the Session has no network, never an open one. `asb check` shows the network line; `asb doctor` checks the daemon and images.

Container hardening is fixed: read-only root filesystem, non-root user, all capabilities dropped (Netguard keeps only `NET_ADMIN`), `no-new-privileges`, never privileged, no Docker socket.

### Host Services

A Host Service is an app on your host the agent may reach, one `host:port` at a time (examples: DBX Web, an LLM gateway). Define it under `network.host_services`, then:

```bash
asb service enable dbx-web      # opens that one port in Netguard, live, no restarts
asb service disable dbx-web     # closes it
```

Activation lasts until `disable` or `asb down`. Every other port on the same host stays blocked. No host credentials pass through. `asb doctor` reports whether each Host Service answers (from the Session when it is enabled in a running Session, otherwise from the host). Host Services can return sensitive data: the agent can read whatever the service answers.

### Portals

A Portal is a service inside the Session that you open from the host. It is published only on `127.0.0.1`, on a random host port; traffic starts on the host and goes in. Nothing is published by `asb up`.

```bash
asb portal start plannotator          # prints the URL; asb never opens a browser
asb portal status plannotator         # publication and service state, reported separately
asb portal restart plannotator
asb portal stop plannotator           # releases the host port; the URL may change next time
```

- Without a `command`, the agent starts the service itself (Plannotator is started by the agent's plan hook, with `PLANNOTATOR_REMOTE=1`) and asb only publishes the port. With a `command`, `portal start` also runs it in the Session.
- `hermes-dashboard` listens on `0.0.0.0` inside the Session, so Hermes requires a login. The login is generated once per Workspace with `{generated}`, stored under `$XDG_STATE_HOME/agent-sandbox/portal-secrets/` (default `~/.local/state/...`, mode 0600, outside the Workspace) and printed by `portal start`. `status`, `ls`, `check` and `up` never print it. The agent can read the credentials and reach the dashboard; the dashboard only controls Hermes in the same Session.
- `asb down` unpublishes every Portal; after the next `asb up` each one is off until started again.
- `asb ls` lists the URLs of started Portals.

## Host Browser

The Host Browser is a Chrome on your host that the agent drives from the Session with `asb-chrome-mcp` (which runs `chrome-devtools-mcp`). It is off by default. Unlike a Host Service it lets the agent reach whatever the host can: the intranet and `file:///`. Make it available only from global config or a flag; project config cannot:

```yaml
version: 1
network:
  host_browser: true
```

```bash
asb doctor --host-browser        # prints the Chrome launch command (and on Linux a socat relay)
asb up --host-browser            # warns on every Session where it is available
asb browser enable --host-browser   # opens host.docker.internal:9222, one TCP port, in Netguard
asb browser disable
```

You start Chrome yourself; asb never starts host programs. `asb doctor` prints the exact command, which uses a dedicated profile so none of your cookies or logins are exposed:

```text
google-chrome --remote-debugging-port=9222 --user-data-dir=$XDG_STATE_HOME/agent-sandbox/chrome-profile --no-first-run
```

On macOS the binary is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. On Linux Chrome listens on `127.0.0.1` only, which the Session cannot reach, so `doctor` also prints a `socat` relay bound to the Docker gateway address (never `0.0.0.0`) that you run yourself. `check`, `up` and `attach` print a warning each time the Host Browser is available. The Host Browser, when enabled, exposes the host's whole network view and host files to the agent.

## herdr and Kanban workflow

- **herdr** is the Watcher. It runs inside the Session; `asb attach` runs `docker exec -it` into it. It never connects to your host herdr, and no herdr socket is mounted. Agents are panes of this herdr and share the Session's masks, Netguard and Portals. herdr's Hermes and OMP integrations are installed in their Agent volumes by `up`/`attach` on a fresh start and by `asb update`. Detach with herdr's own keys; the Session keeps running until `asb down`.
- **Kanban** is for Hermes only. Each Workspace has its own Kanban home volume, `agent-sandbox-kanban-<slug>`, and the Session sets `HERMES_KANBAN_HOME=/kanban` and `HERMES_KANBAN_BOARD=<slug>`. Boards and tasks persist across Sessions; `asb down` keeps the volume. OMP and the other agents are not wired to it.
- A typical day: `asb up`, `asb portal start hermes-dashboard` to see the Kanban board in your browser (`portal start` prints the login), `asb attach` to watch agents, `asb down` when done.

The volumes `agent-sandbox-<agent>`, `agent-sandbox-mise` and `agent-sandbox-kanban-<slug>` are never removed by `down` or `update`. To log an agent out, remove its volume yourself with `docker volume rm`.

## Update and rollback

```bash
asb update                      # CLI, then images, then herdr integrations
asb update --only image         # one step: cli, image or integrations
asb update --rollback           # point `latest` back at the previous build
```

`update` upgrades the CLI with `uv tool upgrade agent-sandbox` or `pipx upgrade agent-sandbox` (if you installed it another way it warns and skips that step), rebuilds the agent and Netguard images with no cache at the latest versions, and re-runs the herdr integration install in each Agent volume. Each build is tagged with its date; `latest` moves only after both images built, and the build it replaced is kept as `previous`. Rollback goes back one build only. Running Sessions keep their image and are never stopped; `update` lists them with "restart to apply: asb down && asb up". Two machines updated on different days can have different agent versions.

## Known limits

This page is the same list `asb check` prints under `Limits:`. These are limits of the tool, not bugs to be fixed by a config change.

1. Masking is by filename only; secrets inside other files are not detected.
2. Secrets committed to git history stay readable.
3. Files created after `up`, or under Skip patterns, are not masked.
4. Hard links are found only by scanning the Workspace outside Skip patterns; a hard link under a Skip pattern or outside the Workspace is not masked.
5. The agent can change Workspace files that later run on the host (Makefile, package.json, .git/hooks).
6. Host Services can return sensitive data; the Host Browser, when enabled, exposes the host's whole network view and host files.
7. The public internet stays open: the agent can send Workspace data to any public IP address (Netguard blocks the host and private networks, not exfiltration).
8. Containers share the host kernel.
9. Agent credentials in Agent volumes are readable by the agent and by anything it runs.
10. The inner herdr can set the terminal title and request clipboard writes (OSC 52); turn off clipboard writes from programs in your terminal.

What that means in practice:

- **Filename-only masking.** A token pasted into `notes.md` or `config.py` is not masked. Add a Secret pattern (`secrets.mask`) for files you know about.
- **Git history.** Mask applies to the working tree. Anything already committed can be read with `git log -p`.
- **Late files and Skip patterns.** Masks are fixed when the Session starts. A `.env` created afterwards is readable and writable; `asb attach` warns about new matches on a running Session, and `asb down` then `asb up` masks them. Nothing under `node_modules/`, `vendor/`, `dist/` or `libs/` (or your own Skip patterns) is scanned.
- **Files that run later on the host.** The Workspace is read-write. An agent that is hijacked, or simply wrong, can edit a `Makefile`, `package.json` scripts, a Git hook or a CI file, and you will run it on the host. This tool does not contain a hijacked or malicious agent. Review diffs before running anything.
- **Host Services and Host Browser.** A Host Service is only as safe as what it returns. The Host Browser is much broader: it is host Chrome, so the agent can load intranet pages and `file:///` URLs. Enable it only when you need it, with the dedicated profile.
- **Open internet.** Netguard is not an exfiltration control.
- **Shared kernel.** A container is not a virtual machine. A kernel or Docker escape is outside what this tool protects against.
- **Agent credentials.** Agent logins live in Agent volumes, which the agent and anything it runs can read. Portal logins generated by asb are readable by the agent too.
- **OSC 52 clipboard.** The herdr inside the Session renders through your terminal. Disable clipboard writes from programs in your terminal's settings.

### What is verified, and what is not

Everything that needs a real Docker daemon is documented from design. The Seam 2 end-to-end tests (`tests/test_e2e_docker.py` and the other `tests/test_e2e_*.py`, marked `docker`) are not yet run against a real Docker daemon, on Linux or on Colima. They skip when no daemon is reachable. Run them with `uv run pytest -m docker`. The assumptions behind them are written up in `docs/spikes/`, and every one of those notes is **UNVERIFIED** until someone runs the steps and records the results:

- `docs/spikes/0003-mask-mounts.md`
- `docs/spikes/0004-alias-masking.md`
- `docs/spikes/herdr-in-session.md`
- `docs/spikes/hermes-dashboard.md`
- `docs/spikes/host-browser.md`
- `docs/spikes/host-services.md`
- `docs/spikes/mise-readonly-volume.md`
- `docs/spikes/netguard.md`
- `docs/spikes/portals.md`
- `docs/spikes/update.md`

The design fails closed where it can (a mask that cannot be applied makes `up` fail; a Netguard that fails leaves the Session with no network), but until those tests pass, treat each Docker-dependent claim above as intended behaviour, not observed behaviour.

Known gap: SPEC §3 describes a `shell` command that is not implemented yet. Use `asb attach` and open a shell pane in herdr.

## Development

```bash
uv sync
uv run python -m pytest -q            # Seam 1, no Docker
uv run python -m pytest -m docker     # Seam 2, needs a Docker daemon
```

Use `python -m pytest` rather than the entry point if `/tmp` is mounted `noexec`. Terms are defined in `GLOSSARY.md`, decisions in `docs/adr/`, the specification in `docs/SPEC.md`. The ADRs win over the SPEC where they differ.
