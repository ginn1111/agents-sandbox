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
