# Spike: herdr and its Hermes/OMP integrations inside the Session

**Status: UNVERIFIED.** Written on a host with no Docker daemon; nothing here was run. The design
fails closed: if an assumption is wrong, herdr shows less (no state for a pane, no resume), and no
security control is relaxed. Nothing is made writable, mounted or exposed to compensate.

## Question (ADR-0009, ticket #8)

Inside the Session (`--read-only`, non-root arbitrary `--user`, `HOME=/tmp` tmpfs, no capabilities,
Netguard network), can herdr start, keep running after `asb attach` detaches, and can its official
Hermes and OMP integrations report to herdr's own socket from inside the container?

## Design under test

| Piece | Where | Why |
|---|---|---|
| herdr release binary (`herdr-linux-{x86_64,aarch64}`) | image, `/usr/local/bin/herdr`, `herdr --version` at build | ADR-0009: the version is fixed by the image. |
| `HERDR_CONFIG_PATH=/etc/asb/herdr.toml` (`onboarding = false`) | read-only image layer | herdr's default config dir is under `$HOME` (tmpfs, empty each Session); this skips first-run setup without a writable config. |
| `asb attach` → `docker exec -it -w /workspace <ctr> herdr` | CLI | ADR-0009. No host socket, env var or path is passed. |
| `--init` on the Session | plan | herdr panes leave orphans and zombies; Docker's `tini` reaps them. Adds no capability or mount. |
| `mkdir -p <home>/.hermes` and `<home>/.omp/agent`, then `env HOME=<home> herdr integration install hermes\|omp` via `docker exec` after the Session starts | `up` / `attach` on a fresh start | herdr writes `~/.hermes/plugins/herdr-agent-state/` + `config.yaml`, and `~/.omp/agent/extensions/herdr-omp-agent-state.ts`. Both require the config directory to already exist. The target is the Agent volume (the agent's HOME, ADR-0006), which only exists at run time, so the build cannot do it. Re-running is expected to be idempotent. |
| `/opt/asb/bin/<agent>` → `asb-agent` first on `PATH` | image | A pane's shell inherits `HOME=/tmp`; the wrapper sets `HOME=/agents/<agent>` and execs the real agent, so a bare `hermes` typed in a pane finds its login and the installed plugin. Only `HOME` changes. |

## Things to check first (in order)

1. `docker run --rm --read-only --user 4242:4242 --tmpfs /tmp -e HOME=/tmp <image> herdr --version` works. Fail: the binary needs something the image lacks (glibc is present; check `ldd`).
2. `asb attach`, press `ctrl+b q`: the exec client exits 0 and `docker exec <ctr> pgrep -x herdr` still finds the **server**. This depends on herdr daemonising its server out of the exec's process group. If it dies with the client, panes die on detach; the fix is to start the server with `docker exec -d <ctr> herdr server` on `up` (needs the right subcommand from `herdr --help`) — not a security change.
3. Reattach shows the same panes (`herdr pane list` equal before/after). e2e: `test_attach_start_a_pane_detach_reattach_and_see_the_same_pane`.
4. `herdr integration install omp` with `HOME=/agents/omp` writes the extension and `hermes` writes the plugin into `/agents/hermes/.hermes`. Check `PI_CODING_AGENT_DIR`/`PI_CONFIG_DIR` are not set (they are not: no host env is passed). If the OMP agent directory is not `~/.omp/agent` for the pinned OMP, adjust `INTEGRATION_DIRS` in `src/agent_sandbox/herdr.py`.
5. The key question: a Hermes/OMP pane's integration finds herdr's socket. herdr gives pane children `HERDR_SOCKET_PATH` and the pane id; the socket is created under herdr's runtime/config dir under `HOME=/tmp` (writable tmpfs). With the wrapper, the agent's `HOME` differs from herdr's, so check that the plugin uses `HERDR_SOCKET_PATH` and does not recompute the path from `$HOME`. If it recomputes it, the wrapper would break status; the alternative is to also export `HERDR_SOCKET_PATH` (herdr sets it already) or to point herdr's socket at a fixed path with `HERDR_SOCKET_PATH`.
6. herdr shows working / idle / blocked for a Hermes pane and an OMP pane. OMP's state comes only from its integration (lifecycle authority), Hermes's from screen detection plus the integration's session id. Check `herdr integration status` and the sidebar.
7. The agent's own cwd and `$HOME`: the wrapper must not break `claude`/`codex` logins (same `HOME=/agents/<name>` as `launch` in the plan).
8. `herdr` writes its config/state under `HOME=/tmp`; confirm nothing tries to write elsewhere on the read-only root (`herdr` logs in `~/.config/herdr/herdr.log`).
9. Latest herdr might ignore `HERDR_CONFIG_PATH` or try auto-update (`herdr update` is for installer-managed installs; ours is a direct binary download). If it prompts or phones home, set the relevant `[update]` option in `/etc/asb/herdr.toml`. The Session has the public internet, so that is not a new exposure.

## Host herdr is unreachable

- The plan mounts only the Workspace bind plus named volumes; no `*.sock` path and no `HERDR_*` variable is passed (`tests/test_herdr.py`). The `HOME` of the host is never mounted.
- A Workspace could contain a file named like a socket, but a plain file is not a connectable host socket, and a socket bind-mounted from the host cannot appear unless the user places it in the Workspace. Unix sockets inside a Workspace on the host are outside what the tool can see (it never reads contents); a host herdr's socket normally lives under `~/.config/herdr` or `$XDG_RUNTIME_DIR`, neither of which is mounted.

## Known limits

- The inner herdr can set the terminal title and request clipboard writes (OSC 52) through the human's terminal; already in `LIMITS` and SPEC §11.
- If the integration install fails, `up` warns and keeps the Session: only status/resume for that agent is lost.
- Concurrent Sessions run `integration install` on the same Agent volume at once; the files are written atomically by herdr, and the content is identical.
