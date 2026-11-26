"""Seam 2: Portals on a real Docker daemon (ADR-0010, ADR-0013, ADR-0018). Skipped without one.

UNVERIFIED: written on a host with no Docker daemon, so none of this has run. The assumptions behind
it are listed in docs/spikes/portals.md. Run `uv run python -m pytest -m docker tests/test_e2e_portals.py`
on Linux Docker and on Colima, then replace each **Result:** line in the spike with what you saw.
"""
import json
import re
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

from agent_sandbox import portals
from agent_sandbox.cli import main
from agent_sandbox.plan import session_name

pytestmark = pytest.mark.docker


def _docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


if not _docker_ok():
    pytest.skip("no reachable Docker daemon", allow_module_level=True)


def dx(name, *argv, detach=False):
    return subprocess.run(["docker", "exec", *(["-d"] if detach else []), name, *argv], capture_output=True, text=True)


def docker(*argv):
    return subprocess.run(["docker", *argv], capture_output=True, text=True)


def start_portal(ws, capsys, name="plannotator"):
    assert main(["portal", "start", name, str(ws)]) == 0
    out = capsys.readouterr().out
    m = re.search(rf"^{name} → (http://127\.0\.0\.1:(\d+)\S*)$", out, re.M)
    assert m, out
    return m.group(1), int(m.group(2))


def serve_in_session(session, port):
    """A stand-in for the service the agent starts: python's http.server on 0.0.0.0, in the Session."""
    dx(session, "python3", "-m", "http.server", str(port), "--bind", "0.0.0.0", detach=True)
    for _ in range(50):
        if dx(session, "bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{port}").returncode == 0:
            return
        time.sleep(0.2)
    pytest.fail("the stand-in service did not start")


def fetch(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


def lan_ip():
    """An address of this machine that is not loopback, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


@pytest.fixture
def session(workspace):
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_plain_up_publishes_nothing(session):
    for container in (session, f"{session}-netguard"):
        ports = json.loads(docker("inspect", "--format", "{{json .HostConfig.PortBindings}}", container).stdout)
        assert not ports
    assert docker("ps", "--filter", "label=agent-sandbox=portal", "-q").stdout.strip() == ""


def test_a_started_portal_answers_on_127_0_0_1_and_a_new_port_each_time(session, workspace, capsys):
    serve_in_session(session, 19432)
    url, _ = start_portal(workspace, capsys)
    assert fetch(url) == 200


def test_a_portal_is_not_reachable_from_other_machines(session, workspace, capsys):
    serve_in_session(session, 19432)
    _, host_port = start_portal(workspace, capsys)
    publisher = portals.publisher_name(session, "plannotator")
    bound = docker("port", publisher, "8000/tcp").stdout.split()
    assert bound and all(b.startswith("127.0.0.1:") for b in bound)
    ip = lan_ip()
    if ip:  # on a host with a LAN address, that address must refuse the connection
        with pytest.raises(OSError):
            socket.create_connection((ip, host_port), timeout=3).close()


def test_two_sessions_get_different_host_ports(home, capsys):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert main(["build"]) == 0
    try:
        assert main(["up", str(a)]) == 0 and main(["up", str(b)]) == 0
        _, pa = start_portal(a, capsys)
        _, pb = start_portal(b, capsys)
        assert pa != pb
    finally:
        main(["down", str(a)])
        main(["down", str(b)])


def test_portals_are_one_way_nothing_in_the_session_reaches_the_publisher(session, workspace, capsys):
    start_portal(workspace, capsys)
    publisher = portals.publisher_name(session, "plannotator")
    net = portals.network_name(session, "plannotator")
    info = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", publisher).stdout)
    assert net in info
    for network, v in info.items():  # the publisher listens on 8000 on every address it has
        r = dx(session, "bash", "-c", f"exec 3<>/dev/tcp/{v['IPAddress']}/8000")
        assert r.returncode != 0, f"the Session reached the publisher on {network}"
    # and the Session's own view of its network has no way to the portal network's other members
    assert dx(session, "bash", "-c", "exec 3<>/dev/tcp/host.docker.internal/19432").returncode != 0


def test_stop_releases_the_host_port_and_start_works_again(session, workspace, capsys):
    serve_in_session(session, 19432)
    url, host_port = start_portal(workspace, capsys)
    assert main(["portal", "stop", "plannotator", str(workspace)]) == 0
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", host_port), timeout=2).close()
    assert fetch(start_portal(workspace, capsys)[0]) == 200


def test_start_does_not_restart_the_session_or_netguard(session, workspace, capsys):
    ids = {n: docker("inspect", "--format", "{{.Id}} {{.State.StartedAt}}", n).stdout for n in (session, f"{session}-netguard")}
    start_portal(workspace, capsys)
    assert ids == {n: docker("inspect", "--format", "{{.Id}} {{.State.StartedAt}}", n).stdout for n in ids}


def test_down_unpublishes_every_portal(session, workspace, capsys):
    start_portal(workspace, capsys)
    assert main(["down", str(workspace)]) == 0
    assert docker("ps", "-a", "--filter", "label=agent-sandbox=portal", "-q").stdout.strip() == ""
    assert docker("network", "ls", "--filter", "label=agent-sandbox=portal-network", "-q").stdout.strip() == ""


def test_the_session_still_has_no_route_to_the_host_while_a_portal_is_published(session, workspace, capsys):
    start_portal(workspace, capsys)
    assert dx(session, "bash", "-c", "exec 3<>/dev/tcp/host.docker.internal/22").returncode != 0
    assert dx(session, "bash", "-c", "timeout 5 curl -sS -m 4 -o /dev/null https://example.com").returncode == 0  # internet stays open


def test_plannotator_is_in_the_image_and_configured_for_remote_use(session):
    assert dx(session, "plannotator", "--version").returncode == 0
    env = dx(session, "env").stdout
    assert "PLANNOTATOR_REMOTE=1" in env and "PLANNOTATOR_PORT=19432" in env
    assert dx(session, "test", "-f", "/etc/claude-code/managed-settings.json").returncode == 0


def test_a_command_portal_is_started_by_asb_and_stopped_with_it(workspace, capsys):
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nportals: {docs: {port: 8080, command: [python3, -m, http.server, '8080', --bind, 0.0.0.0]}}\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    try:
        url, _ = start_portal(workspace, capsys, "docs")
        assert fetch(url) == 200
        assert main(["portal", "stop", "docs", str(workspace)]) == 0
        assert dx(name, "bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8080").returncode != 0  # the process is gone
    finally:
        main(["down", str(workspace)])
