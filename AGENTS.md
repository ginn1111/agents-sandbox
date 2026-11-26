# Agent Sandbox — agent instructions

`agent-sandbox` (alias `asb`) is a planned Python CLI that runs AI coding agents in Docker so they can only touch one Workspace directory and can't read the Secret files inside it. **The repo has no code yet and no commits:** it holds only the design (glossary, spec, ADRs, one implementation issue). The first job is building the CLI from these docs.

## Source of truth (read before coding)

- `GLOSSARY.md`: required vocabulary. Use the terms exactly as written (Workspace, Secret file, Secret pattern, Skip pattern, Mask, Portal, Host Service, Host Browser, Watcher, Session, Netguard, Agent volume). Don't use the words listed under each `_Avoid_:` line.
- `docs/adr/0001`–`0012`: decisions and their reasons. **ADRs win** over `docs/SPEC.md` and over `docs/issues/0001-agent-sandbox.md`.
- `docs/SPEC.md`: reference for commands, default Secret/allow/Skip patterns, container defaults, example `check` output, example config.
- `docs/issues/0001-agent-sandbox.md`: the implementation issue (`ready-for-agent`). It has user stories, module breakdown and testing decisions.

## Stack (ADR-0011, SPEC §2)

- Python 3.11+, standard library plus one YAML parser. Installed with `uv tool install` or `pipx`. Two entry points: `agent-sandbox` and `asb`.
- Docker only through the `docker` CLI as a subprocess with **argument lists, never shell strings**. No Docker SDK. Use the user's current Docker context (Linux Docker, Colima, Docker Desktop).
- Netguard: an Alpine image with `iptables` and a shell script. Agent image: one Dockerfile for all agents. Toolchains come from mise.
- Setup: `uv sync` (creates `.venv`, installs PyYAML and pytest; build backend is hatchling, source in `src/agent_sandbox/`).
- Test: `uv run pytest` (Seam 1, no Docker). Seam 2 end-to-end tests are in `tests/test_e2e_docker.py`, marked `docker`, and skip when no Docker daemon is reachable. Run only them with `uv run pytest -m docker`.
- Run: `uv run asb <command>` or `uv run python -m agent_sandbox.cli <command>`. Install globally with `uv tool install .` or `pipx install .`.
- Tests that touch Docker use `tests/fake_docker.py` (a recording stand-in passed via `main(argv, docker=[...])`); never call a real `docker` from Seam 1 tests.
- If `/tmp` is mounted `noexec`, `.venv/bin/asb` cannot run; use `python -m agent_sandbox.cli` or `python -m pytest`.
- No lint command is configured yet.

## Planned module layout (issue 0001)

- **Config**: merge with precedence CLI > project `.agent-sandbox.yaml` > global `~/.config/agent-sandbox/config.yaml` > defaults. Reject unknown or invalid keys. Record where each rule came from.
- **Discovery**: apply Secret, allow and Skip globs. Follow symlinks with secret names to their targets inside the Workspace, and find every hard link to a matched inode. Never follow a link out of the Workspace. Never read file contents.
- **Planner (main seam)**: `plan(workspace, config) -> SessionPlan`. A pure function with no Docker calls. `asb check` renders it and `asb check --json` serialises it.
- **Runtime**, **Netguard**, **Image**, **Lifecycle** (`build`/`update`/rollback), **Doctor**.
- Build order: config → discovery → planner and `check` → runtime (`up`/`down`/`attach`/`ls`) → Netguard and `doctor` → image, volumes and `update` → Portals → Host Browser → README. Do one step at a time and run its tests before moving on.

## Testing (issue 0001, SPEC §12)

- Tests check external behaviour only: the SessionPlan, or what a real container can see and reach. Don't test private helpers or call order.
- Seam 1 (most tests): call `plan()` or `asb check --json` on fixture Workspaces built in temp dirs. No Docker, so they run fast.
- Seam 2: end-to-end `asb up`/`down` on real Docker (Linux and Colima), probing with `docker exec`. Use it only for what the plan can't prove.
- Some ADR assumptions still need spikes before you rely on them: symlinked mount targets, the Hermes dashboard Host-header check, `HERMES_KANBAN_BOARD` isolation, herdr integrations in the container, mise with a read-only root, Netguard DNS on Colima.

## Hard rules

- **Never weaken a security control** to get around a compatibility problem. Record the trade-off in a new ADR (`docs/adr/NNNN-kebab-title.md`, next number 0013) using the same shape: title as the decision, a paragraph, `## Considered Options`, `## Consequences`.
- Never claim protection the tool doesn't give. The README and `check` must state the known limits in SPEC §11.
- Fail closed: if a mask fails, `up` fails. If Netguard fails or stops, the Session has no network.
- Container hardening is fixed (SPEC §4): read-only root, non-root user, all capabilities dropped (Netguard keeps only `NET_ADMIN`), `no-new-privileges`. Never `--privileged`, the Docker socket, host env vars, or host agent config.
- Project config can't enable the Host Browser or set Portal secrets (ADR-0005, ADR-0008).
- The CLI never starts host programs (Chrome) and never opens a browser. It prints URLs only.

## Pitfalls

- The SPEC §13 build order is older and differs slightly from the issue. Follow the issue's order, which puts planner and `check` before Netguard.
- `**/.git/config` is masked by default, so git inside the sandbox has no remotes. Tests must confirm local status, diff and commit still work.
- `.git/` is not a Skip pattern. Only `node_modules/`, `vendor/`, `dist/` and `libs/` are skipped by default.
- Portal ports and `--add-host` go on the **Netguard** container, not the Session, because the Session uses `--network container:<netguard>`.
- Git remote: `git@github.com:ginn1111/agents-sandbox.git`, branch `main`. No commit convention is established yet.
