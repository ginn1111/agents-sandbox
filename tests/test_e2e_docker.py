"""Seam 2: end-to-end against a real Docker daemon. Skipped when `docker` is unavailable.

Probes what an agent inside the Session can actually see. Run with `uv run pytest -m docker`.
"""
import ipaddress
import shutil
import socket
import subprocess
import threading

import pytest

from agent_sandbox.cli import IMAGE_CONTEXT, main
from agent_sandbox.netguard import READY_FILE
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


# --- Netguard (ADR-0002, ADR-0003).
# UNVERIFIED: written on a host with no Docker daemon, so none of these has run. The assumptions they
# check are listed in docs/spikes/netguard.md. Run them on Linux Docker and on Colima.


def netguard_of(name):
    return f"{name}-netguard"


def docker_ps_names():
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    return out.split()


def reaches(name, host, port, seconds=3):
    """Can the container open a TCP connection? bash's /dev/tcp, because the image has no nc."""
    script = f"timeout {seconds} bash -c 'exec 3<>/dev/tcp/{host}/{port}'"
    return subprocess.run(["docker", "exec", name, "sh", "-c", script], capture_output=True).returncode == 0


def default_gateway(name):
    """The Session's default gateway, read from /proc/net/route (little-endian hex)."""
    out = sh(name, "awk '$2 == \"00000000\" { print $3; exit }' /proc/net/route").stdout.strip()
    return ".".join(str(b) for b in bytes.fromhex(out)[::-1])


@pytest.fixture
def host_listener():
    """A TCP server on the host, bound to every interface. Yields its port."""
    server = socket.socket()
    server.bind(("0.0.0.0", 0))
    server.listen(16)
    server.settimeout(0.2)
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                server.accept()[0].close()
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield server.getsockname()[1]
    stop.set()
    t.join(timeout=2)
    server.close()


def host_lan_address():
    """The host's outbound address if it is private; None when the host has no private address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 9))  # sends nothing; only selects a source address
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return addr if ipaddress.ip_address(addr).is_private else None


def test_session_has_public_internet_and_dns(session):
    assert sh(session, "getent hosts example.com").returncode == 0  # DNS
    assert reaches(session, "1.1.1.1", 443)  # the public internet


def test_host_gateway_and_host_addresses_are_unreachable(session, host_listener):
    assert not reaches(session, default_gateway(session), host_listener)
    mapped = sh(session, "getent hosts host.docker.internal").stdout.split()
    if mapped:  # --add-host on Linux: a name alone must not grant access
        assert not reaches(session, mapped[0], host_listener)
    lan = host_lan_address()
    if lan:
        assert not reaches(session, lan, host_listener)


@pytest.mark.parametrize("target", ["10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "100.64.0.1"])
def test_private_link_local_and_cgnat_ranges_are_unreachable(session, target):
    assert not reaches(session, target, 80, seconds=2)


def test_ipv6_is_disabled_in_the_session_network(session):
    assert sh(session, "cat /proc/sys/net/ipv6/conf/all/disable_ipv6").stdout.strip() == "1"
    assert sh(session, "cat /proc/net/if_inet6").stdout.strip() == ""  # no IPv6 address on any interface


def test_session_has_no_capabilities_and_cannot_change_the_rules(session):
    status = sh(session, "grep -E '^Cap(Eff|Prm|Bnd|Inh)' /proc/self/status").stdout
    assert status and all(line.split()[1] == "0000000000000000" for line in status.splitlines()), status
    assert sh(session, "command -v iptables").returncode != 0
    assert sh(session, "grep -q 'NoNewPrivs:.1' /proc/self/status").returncode == 0


def test_netguard_alone_has_net_admin(session):
    status = subprocess.run(["docker", "exec", netguard_of(session), "sh", "-c", "grep CapEff /proc/self/status"],
                            capture_output=True, text=True).stdout
    assert int(status.split()[1], 16) == 1 << 12  # CAP_NET_ADMIN and nothing else


def test_netguard_reports_ready_through_its_ready_file(session):
    probe = subprocess.run(["docker", "exec", netguard_of(session), "test", "-f", READY_FILE], capture_output=True)
    assert probe.returncode == 0


def test_killing_netguard_leaves_the_session_with_no_network(session):
    assert reaches(session, "1.1.1.1", 443)
    subprocess.run(["docker", "kill", netguard_of(session)], check=True, capture_output=True)
    assert not reaches(session, "1.1.1.1", 443)
    assert sh(session, "getent hosts example.com").returncode != 0


def test_netguard_without_net_admin_never_becomes_ready():
    """What a host that refuses NET_ADMIN would give: the script must exit non-zero, not idle."""
    from agent_sandbox.netguard import RULES_ENV, rules
    assert main(["build"]) == 0
    p = subprocess.run(
        ["docker", "run", "--rm", "--cap-drop", "ALL", "-e", f"{RULES_ENV}={';'.join(rules())}",
         "agent-sandbox-netguard:latest"],
        capture_output=True, text=True, timeout=60)
    assert p.returncode != 0


def test_offline_session_has_only_loopback_and_no_netguard(workspace):
    assert main(["build"]) == 0
    assert main(["up", "--offline", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    try:
        assert sh(name, "ls /sys/class/net").stdout.split() == ["lo"]
        assert not reaches(name, "1.1.1.1", 443)
        assert netguard_of(name) not in docker_ps_names()
    finally:
        main(["down", str(workspace)])


def test_down_removes_netguard_too(workspace):
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    assert main(["down", str(workspace)]) == 0
    names = docker_ps_names()
    assert name not in names and netguard_of(name) not in names


def test_concurrent_sessions_have_their_own_netguard(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    na, nb = session_name(a.resolve()), session_name(b.resolve())
    try:
        assert main(["build"]) == 0
        assert main(["up", str(a)]) == 0 and main(["up", str(b)]) == 0
        subprocess.run(["docker", "kill", netguard_of(na)], check=True, capture_output=True)
        assert not reaches(na, "1.1.1.1", 443)
        assert reaches(nb, "1.1.1.1", 443)  # B keeps its network: its Netguard is its own
    finally:
        main(["down", str(a)])
        main(["down", str(b)])
