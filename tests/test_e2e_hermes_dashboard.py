"""Seam 2: the Hermes dashboard Portal and per-Workspace Kanban on a real Docker daemon (ticket 09,
ADR-0010, ADR-0020). Skipped without one.

UNVERIFIED: written on a host with no Docker daemon, so none of this has run. The assumptions behind it
are listed in docs/spikes/hermes-dashboard.md. Run
`uv run python -m pytest -m docker tests/test_e2e_hermes_dashboard.py` on Linux Docker and on Colima,
then replace each **Result:** line in the spike with what you saw.
"""
import base64
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from agent_sandbox import generated
from agent_sandbox.cli import main
from agent_sandbox.plan import plan, session_name
from agent_sandbox.config import load_config

pytestmark = pytest.mark.docker

DASH = "hermes-dashboard"


def _docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


if not _docker_ok():
    pytest.skip("no reachable Docker daemon", allow_module_level=True)


def dx(name, *argv):
    return subprocess.run(["docker", "exec", name, *argv], capture_output=True, text=True)


def start_dashboard(ws, capsys):
    assert main(["portal", "start", DASH, str(ws)]) == 0
    out = capsys.readouterr().out
    m = re.search(rf"^{DASH} → (http://127\.0\.0\.1:(\d+)\S*)$", out, re.M)
    login = re.search(r"username:\s*(\S+)\s+password:\s*(\S+)", out)
    assert m and login, out
    return m.group(1), login.group(1), login.group(2)


def get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def wait_for(url, ok=lambda s: s < 500, tries=60):
    for _ in range(tries):
        try:
            status, body = get(url)
            if ok(status):
                return status, body
        except OSError:
            pass
        time.sleep(1)
    pytest.fail(f"{url} never answered")


@pytest.fixture
def session(workspace):
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    yield session_name(workspace.resolve())
    main(["down", str(workspace)])


def test_the_dashboard_accepts_the_random_loopback_host_header(session, workspace, capsys):
    url, _, _ = start_dashboard(workspace, capsys)
    status, body = wait_for(url)
    assert status != 400 and "Invalid Host header" not in body  # spike item 1


def test_the_dashboard_serves_its_ui_from_the_image(session, workspace, capsys):
    url, _, _ = start_dashboard(workspace, capsys)
    wait_for(url)
    assert dx(session, "test", "-f", "/opt/hermes/src/hermes_cli/web_dist/index.html").returncode == 0  # spike item 5


def test_the_dashboard_requires_the_generated_login(session, workspace, capsys):
    url, user, password = start_dashboard(workspace, capsys)
    wait_for(url)
    base = url.rstrip("/")
    status, _ = get(base + "/api/status")  # a protected route: no session, no data
    assert status in (301, 302, 303, 307, 401, 403)
    for path in ("/api/sessions", "/api/config"):
        assert get(base + path)[0] != 200
    req = urllib.request.Request(base + "/auth/password-login", method="POST",
                                 data=f'{{"username": "{user}", "password": "wrong"}}'.encode(),
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code in (400, 401, 403)
    ok = urllib.request.Request(base + "/auth/password-login", method="POST",
                                data=f'{{"username": "{user}", "password": "{password}"}}'.encode(),
                                headers={"Content-Type": "application/json"})
    assert urllib.request.urlopen(ok, timeout=10).status in (200, 204)  # spike item 6; the browser part is by hand


def test_the_dashboard_runs_with_the_hermes_agent_volume_as_home(session, workspace, capsys):
    start_dashboard(workspace, capsys)
    cmdline = dx(session, "sh", "-c", "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline; echo; done").stdout
    assert "hermes dashboard --host 0.0.0.0" in cmdline
    pid = dx(session, "cat", "/tmp/asb-portal-hermes-dashboard.pid").stdout.strip()
    env = dx(session, "sh", "-c", f"tr '\\0' '\\n' < /proc/{pid}/environ").stdout
    assert "HOME=/agents/hermes" in env  # spike item 7


def test_the_session_kanban_state_is_in_the_workspaces_own_volume(session, workspace):
    p = plan(workspace, load_config(workspace))
    env = dx(session, "env").stdout
    assert "HERMES_KANBAN_HOME=/kanban" in env and f"HERMES_KANBAN_BOARD={p.kanban['board']}" in env
    assert dx(session, "sh", "-c", "touch /kanban/probe && rm /kanban/probe").returncode == 0  # spike item 4
    boards = dx(session, "hermes", "kanban", "boards", "list").stdout
    assert p.kanban["board"] in boards
    found = dx(session, "sh", "-c", "find /kanban /agents/hermes -name 'kanban*.db' 2>/dev/null").stdout
    assert "/kanban/" in found and "/agents/hermes" not in found  # spike item 2: nothing in the shared volume


def test_up_creates_the_workspace_board_and_it_persists_across_down_and_up(session, workspace):
    p = plan(workspace, load_config(workspace))
    dx(session, "hermes", "kanban", "--board", p.kanban["board"], "create", "survives down", "--assignee", "nobody")
    assert main(["down", str(workspace)]) == 0
    assert main(["up", str(workspace)]) == 0
    out = dx(session, "hermes", "kanban", "--board", p.kanban["board"], "list").stdout
    assert "survives down" in out  # spike item 3


def test_a_board_of_another_workspace_is_not_visible(session, workspace, home):
    other = home / "other" / "api"
    other.mkdir(parents=True)
    assert main(["up", str(other)]) == 0
    try:
        mine = plan(workspace, load_config(workspace)).kanban
        theirs = plan(other, load_config(other)).kanban
        o_session = session_name(other.resolve())
        dx(o_session, "hermes", "kanban", "--board", theirs["board"], "create", "other workspace task", "--assignee", "nobody")
        assert theirs["board"] not in dx(session, "hermes", "kanban", "boards", "list").stdout
        assert "other workspace task" not in dx(session, "hermes", "kanban", "--board", mine["board"], "list").stdout
        assert mine["volume"] != theirs["volume"]  # spike item 8
    finally:
        main(["down", str(other)])


def test_the_login_is_stored_outside_the_workspace_with_private_permissions(session, workspace, capsys):
    _, _, password = start_dashboard(workspace, capsys)
    path = generated.store_path(session)
    assert path.stat().st_mode & 0o077 == 0 and password in path.read_text()
    assert not any(password in p.read_text(errors="ignore") for p in workspace.rglob("*") if p.is_file())
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert password not in ps  # never on a host command line
