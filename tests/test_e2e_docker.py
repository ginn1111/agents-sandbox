"""Seam 2: end-to-end against a real Docker daemon. Skipped when `docker` is unavailable.

Probes what an agent inside the Session can actually see. Run with `uv run pytest -m docker`.
"""
import shutil
import subprocess

import pytest

from agent_sandbox.cli import IMAGE_CONTEXT, main
from agent_sandbox.plan import IMAGE, session_name

pytestmark = pytest.mark.docker


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


if not _docker_ok():
    pytest.skip("no reachable Docker daemon", allow_module_level=True)


def sh(name, script):
    return subprocess.run(["docker", "exec", name, "sh", "-c", script], capture_output=True, text=True)


@pytest.fixture
def session(workspace):
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_workspace_edits_persist_and_home_is_invisible(session, workspace, home):
    (workspace / "a.txt").write_text("host")
    assert sh(session, "cat /workspace/a.txt").stdout == "host"
    sh(session, "echo agent > /workspace/b.txt")
    assert (workspace / "b.txt").read_text().strip() == "agent"
    assert sh(session, f"ls {home}").returncode != 0
    assert sh(session, "ls /workspace/..").returncode == 0  # container root, not host parent
    assert "user" not in sh(session, "ls /home").stdout
    assert sh(session, "test -e /var/run/docker.sock").returncode != 0
    assert sh(session, "id -u").stdout.strip() != "0"
    assert sh(session, "touch /usr/x").returncode != 0  # read-only root


def test_two_sessions_do_not_collide(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    try:
        assert main(["build"]) == 0
        assert main(["up", str(a)]) == 0 and main(["up", str(b)]) == 0
    finally:
        main(["down", str(a)])
        main(["down", str(b)])


# ---------------------------------------------------------------------------- ticket 06
# UNVERIFIED: written on a host without a Docker daemon, so these have never run.
import json
import uuid

from agent_sandbox.plan import agent_home, agent_volume


def dx(name, *argv, user=None, workdir=None):
    cmd = ["docker", "exec"] + (["-u", user] if user else []) + (["-w", workdir] if workdir else [])
    return subprocess.run(cmd + [name, *argv], capture_output=True, text=True)


def _fresh_agent_volume(agent):
    """A throwaway Agent volume with a writable root, as the image provides for shipped agents.

    Config-defined agents are not in the image, so Docker would create their volume root-owned.
    The tests use throwaway agent names so they never touch a real login volume.
    """
    vol = agent_volume(agent)
    subprocess.run(["docker", "volume", "create", vol], check=True, capture_output=True)
    subprocess.run(
        ["docker", "run", "--rm", "--user", "0", "--entrypoint", "chmod", "-v", f"{vol}:/h", IMAGE, "0777", "/h"],
        check=True, capture_output=True,
    )
    return vol


@pytest.fixture
def two_agents(workspace):
    """Two throwaway agents configured for the Workspace, with fresh Agent volumes."""
    tag = uuid.uuid4().hex[:6]
    a, b = f"e2ea{tag}", f"e2eb{tag}"
    vols = [_fresh_agent_volume(a), _fresh_agent_volume(b)]
    (workspace / ".agent-sandbox.yaml").write_text(
        f"version: 1\nagents:\n  {a}: {{command: [sh, -c, 'echo $HOME $E2E_VAR'], env: {{E2E_VAR: hello}}}}\n"
        f"  {b}: {{command: [sh, -c, 'echo $HOME']}}\n"
    )
    yield a, b
    main(["down", str(workspace)])
    for v in vols:
        subprocess.run(["docker", "volume", "rm", "-f", v], capture_output=True)


def test_each_agent_starts_from_the_session_with_its_configured_command_env_and_home(two_agents, workspace):
    from agent_sandbox.config import load_config
    from agent_sandbox.plan import plan

    a, b = two_agents
    assert main(["build"]) == 0 and main(["up", str(workspace)]) == 0
    p = plan(workspace.resolve(), load_config(workspace.resolve()))
    by_name = {x["name"]: x for x in p.agents}
    r = dx(p.container_name, *by_name[a]["launch"])
    assert r.stdout.strip() == f"{agent_home(a)} hello"
    r = dx(p.container_name, *by_name[b]["launch"])
    assert r.stdout.strip() == agent_home(b)  # the first agent's env did not leak to the second


def test_login_persists_across_down_up_and_across_workspaces_but_never_between_agents(two_agents, workspace, home):
    a, b = two_agents
    other = home / "elsewhere" / "other-project"
    other.mkdir(parents=True)
    (other / ".agent-sandbox.yaml").write_text((workspace / ".agent-sandbox.yaml").read_text())
    name = session_name(workspace.resolve())

    assert main(["build"]) == 0 and main(["up", str(workspace)]) == 0
    # stands in for `claude login`: a credentials file in the agent's home
    assert dx(name, "sh", "-c", f"echo token-A > {agent_home(a)}/.credentials.json").returncode == 0
    assert dx(name, "cat", f"{agent_home(a)}/.credentials.json").stdout.strip() == "token-A"

    # down/up on the same Workspace keeps it
    assert main(["down", str(workspace)]) == 0
    assert main(["up", str(workspace)]) == 0
    assert dx(name, "cat", f"{agent_home(a)}/.credentials.json").stdout.strip() == "token-A"

    # a different Workspace sees the same login
    other_name = session_name(other.resolve())
    assert main(["up", str(other)]) == 0
    try:
        assert dx(other_name, "cat", f"{agent_home(a)}/.credentials.json").stdout.strip() == "token-A"
    finally:
        main(["down", str(other)])

    # never shared with another agent: b's home is a different, empty volume
    assert dx(name, "test", "-e", f"{agent_home(b)}/.credentials.json").returncode != 0
    assert dx(name, "sh", "-c", f"ls -A {agent_home(b)}").stdout.strip() == ""
    mounts = json.loads(subprocess.run(["docker", "inspect", name, "--format", "{{json .Mounts}}"],
                                       capture_output=True, text=True).stdout)
    sources = {m["Destination"]: m.get("Name") for m in mounts if m["Type"] == "volume"}
    assert sources[agent_home(a)] == agent_volume(a) and sources[agent_home(b)] == agent_volume(b)
    assert agent_volume(a) != agent_volume(b)


def test_down_keeps_the_agent_volumes(two_agents, workspace):
    a, _ = two_agents
    assert main(["build"]) == 0 and main(["up", str(workspace)]) == 0
    assert main(["down", str(workspace)]) == 0
    r = subprocess.run(["docker", "volume", "inspect", agent_volume(a)], capture_output=True)
    assert r.returncode == 0


def test_nothing_from_the_host_agent_config_is_in_the_session(session, workspace, home):
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text("{}")
    for name in ("claude", "codex", "hermes", "omp"):
        r = dx(session, "sh", "-c", f"ls -A {agent_home(name)}")
        assert "settings.json" not in r.stdout
    assert dx(session, "sh", "-c", "echo $HOME; ls -A $HOME").stdout.split() in ([], ["/tmp"])  # tmpfs, empty


def test_image_ships_the_agents_and_base_tools(session):
    for tool in ("claude", "codex", "omp", "hermes", "bru", "dbx-mcp-server", "git", "curl", "bash", "rg", "jq", "mise"):
        assert dx(session, "sh", "-c", f"command -v {tool}").returncode == 0, tool


def test_node_python_and_go_work_out_of_the_box(session):
    for argv in (["node", "--version"], ["python", "--version"], ["go", "version"]):
        r = dx(session, *argv, workdir="/workspace")
        assert r.returncode == 0, (argv, r.stderr)


def test_root_filesystem_stays_read_only_but_mise_volume_is_writable(session):
    assert dx(session, "touch", "/usr/x").returncode != 0
    assert dx(session, "touch", "/etc/mise/x").returncode != 0
    assert dx(session, "sh", "-c", "touch /mise/probe && rm /mise/probe").returncode == 0
    # the spike (docs/spikes/mise-readonly-volume.md): mise can use its volume under a read-only root
    r = dx(session, "mise", "ls", "--installed")
    assert r.returncode == 0 and "node" in r.stdout and "python" in r.stdout and "go" in r.stdout


def _has_network(session):
    return dx(session, "sh", "-c", "curl -fsS -m 5 -o /dev/null https://nodejs.org").returncode == 0


def test_a_project_pinning_another_node_gets_it_through_mise(session, workspace):
    if not _has_network(session):
        pytest.skip("the Session has no network until Netguard exists; mise cannot download a new Node")
    default = dx(session, "node", "--version", workdir="/workspace").stdout.strip()
    pinned = "18" if not default.startswith("v18") else "20"
    (workspace / "mise.toml").write_text(f'[tools]\nnode = "{pinned}"\n')
    assert dx(session, "mise", "trust", workdir="/workspace").returncode == 0
    assert dx(session, "mise", "install", workdir="/workspace").returncode == 0
    r = dx(session, "node", "--version", workdir="/workspace")
    assert r.stdout.strip().startswith(f"v{pinned}"), r.stdout + r.stderr
    # the agents run on their private Node, so they are unaffected by the project's pin
    assert dx(session, "claude", "--version", workdir="/workspace").returncode == 0
    # the new version lives in the shared mise volume, not in the read-only image
    assert dx(session, "sh", "-c", f"ls /mise/installs/node | grep -q '^{pinned}'").returncode == 0
