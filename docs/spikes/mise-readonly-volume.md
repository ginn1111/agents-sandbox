# Spike: mise installs into a volume while the root filesystem is read-only

**Status: UNVERIFIED.** Written on a host with no Docker daemon, so nothing here has been run.
The design below fails closed: if the assumption is wrong, installing a project toolchain fails
with an error. Nothing is made writable to compensate (SPEC §4, AGENTS.md hard rules).

## Question (ADR-0007)

With `--read-only`, a non-root `--user <uid>` that is not baked into the image, `/tmp` and `/run`
as tmpfs and `HOME=/tmp`, can mise:

1. use the global Node LTS, Python and Go installed at build time, and
2. install a different version named by a project's `mise.toml` / `.tool-versions`,

with everything it writes landing in the `agent-sandbox-mise` volume?

## Design under test

| Piece | Where | Why |
|---|---|---|
| `MISE_DATA_DIR=/mise`, `MISE_CACHE_DIR=/mise/cache`, `MISE_STATE_DIR=/mise/state` | image `ENV` | mise's data, downloads, shims, trust state and tool installs all go to the volume, none under `$HOME` (a tmpfs) or `/usr`. |
| Global defaults in `/etc/mise/config.toml` (`MISE_GLOBAL_CONFIG_FILE`) | read-only image layer | mise only reads it, and it is not copied from any host. |
| Build-time `mise install` into `/mise`, then `chmod -R a+rwX /mise` | image | Docker copies a volume mount point's image contents into a **new, empty** named volume, keeping modes, so any `--user` can write and Node/Python/Go work on the first `up`. |
| `/mise` mounted as `type=volume,source=agent-sandbox-mise` | Session | Shared by all Workspaces and all agents; holds only toolchains, no credentials. |
| Private Node at `/opt/node` for the npm agents, shebangs pinned by `wrap-node-bins.sh` | image | A project pinning Node 18 through the shims must not change which Node runs Claude Code, Codex or OMP. |
| `PATH` starts with `/mise/shims` | image `ENV` | A project's pinned versions win for every command the agent runs in `/workspace`. |

## Things to check first (in order)

1. `docker run --rm --read-only --user 4242:4242 --tmpfs /tmp -e HOME=/tmp -v asb-mise-spike:/mise agent-sandbox-agent mise ls --installed` lists node, python and go.
   - Pass: volume was seeded and is writable by an arbitrary uid.
   - Fail: the volume came up root-owned (this also happens if the volume already existed empty). The image would then need an entrypoint that fixes ownership, which needs root and `CAP_CHOWN`. That would be a security trade-off and needs an ADR (next number is 0014) before it is done.
2. Same container, with network: `mise use node@18` in a scratch project, then `node --version` prints `v18…` and `/mise/installs/node/18*` exists. Without network (Netguard absent, the Session has `--network none`) this fails with a download error; that is expected and is what the e2e test skips on.
3. `mise trust` is required for a project `mise.toml` that defines tasks or hooks; the trust record goes to `/mise/state`. Check that `mise install` without `mise trust` refuses, rather than running project code silently.
4. mise needs a writable temp dir during installs; `/tmp` is a tmpfs of limited size. Check a Go and a Python (compiled-from-source fallback) install do not exceed it. Python from mise normally downloads a prebuilt build.
5. Stale global defaults: Docker seeds a volume only when it is empty. After `asb update` rebuilds the image, an existing `agent-sandbox-mise` volume still holds the old Node LTS/Python/Go, and mise installs the new "latest" into it on demand because `/etc/mise/config.toml` says `lts`/`latest`. Check that the old versions do not pile up unbounded (`mise prune` can be run in a Session).

## Expected result, if the assumptions hold

`mise` runs as any uid with a read-only root, and every byte it writes is in `/mise`, `/tmp` or the `Workspace`. The e2e test `test_root_filesystem_stays_read_only_but_mise_volume_is_writable` and `test_a_project_pinning_another_node_gets_it_through_mise` in `tests/test_e2e_docker.py` encode this.

## Other unverified assumptions in the Dockerfile

- `https://mise.run` honours `MISE_INSTALL_PATH`; `mise use --global` writes to `MISE_GLOBAL_CONFIG_FILE`.
- `npm install -g --prefix /opt/agents` of `@anthropic-ai/claude-code`, `@openai/codex`, `@oh-my-pi/pi-coding-agent`, `bun`, `@dbx-app/mcp-server` and `@usebruno/cli` succeeds, and the bin names are `claude`, `codex`, `omp`, `dbx-mcp-server` and `bru` (the e2e test `test_image_ships_the_agents_and_base_tools` checks this). OMP may need `bun`'s postinstall script, which some npm configs block; if so, build fails visibly.
- Hermes installs with `uv pip install -e` from the git checkout into `/opt/hermes/venv`. The official installer was not read (the fetch was blocked); the `hermes` entry point name is assumed.
- Each agent honours `$HOME` for its login and settings (`~/.claude`, `~/.codex`, `~/.omp`, `~/.hermes`). Hermes also honours `HERMES_HOME`. If one agent stores state elsewhere, its login will not persist, and the e2e login test will not catch it because it uses a fake credentials file; check with a real login.
- Concurrent Sessions of the same agent share its volume. Token refresh conflicts are not tested here (ADR-0006 asks for a per-agent test).
- Docker can create the volume mount points `/agents/<name>` and `/mise` under a read-only root. It does this for `/workspace` already (ticket 02), so this is expected to hold.
- `asb up` runs `docker volume create` for each volume before starting the Session; if that fails, `up` fails and nothing starts (fail closed).
