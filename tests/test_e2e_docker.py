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


# --------------------------------------------------------------------------- Secret masking (ticket #3)
#
# UNVERIFIED on the machine this was written on (no Docker daemon). They encode the acceptance criteria
# and must be run with `uv run pytest -m docker` on a host with Docker before the ticket is closed.

import os  # noqa: E402


@pytest.fixture
def secret_session(workspace):
    (workspace / ".env").write_text("TOKEN=abc\n")
    (workspace / "app").mkdir()
    (workspace / "app" / ".env.local").write_text("LOCAL=1\n")
    (workspace / ".env.example").write_text("TOKEN=\n")
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "id_rsa").write_text("PRIVATE\n")
    os.link(workspace / ".env", workspace / "notes.txt")
    (workspace / "config").mkdir()
    (workspace / "config" / "real.secretfile").write_text("REAL\n")
    os.symlink("config/real.secretfile", workspace / "secrets.json")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / ".env").write_text("SKIPPED=1\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_masked_files_read_as_empty_and_templates_stay_readable(secret_session, workspace):
    for rel in (".env", "app/.env.local", "notes.txt", "config/real.secretfile"):
        r = sh(secret_session, f"wc -c < /workspace/{rel}")
        assert r.returncode == 0 and r.stdout.strip() == "0", rel
    assert sh(secret_session, "cat /workspace/.env.example").stdout == "TOKEN=\n"
    assert sh(secret_session, "cat /workspace/node_modules/.env").stdout == "SKIPPED=1\n"  # Skip pattern: not masked


def test_masked_files_cannot_be_written_deleted_or_renamed(secret_session, workspace):
    for rel in (".env", "notes.txt", "config/real.secretfile"):
        assert sh(secret_session, f"echo pwned > /workspace/{rel}").returncode != 0
        assert sh(secret_session, f"rm -f /workspace/{rel}").returncode != 0
        assert sh(secret_session, f"mv /workspace/{rel} /workspace/moved").returncode != 0
        assert sh(secret_session, f"truncate -s 0 /workspace/{rel}").returncode != 0
    assert sh(secret_session, "test -e /workspace/moved").returncode != 0


def test_masked_directories_appear_empty_and_read_only(secret_session):
    assert sh(secret_session, "ls -A /workspace/.ssh").stdout == ""
    assert sh(secret_session, "touch /workspace/.ssh/new").returncode != 0
    assert sh(secret_session, "rmdir /workspace/.ssh").returncode != 0
    assert sh(secret_session, "mv /workspace/.ssh /workspace/ssh2").returncode != 0


def test_host_originals_are_byte_identical_after_up_and_down(workspace):
    files = {".env": b"TOKEN=abc\n", "id_rsa": b"PRIVATE\n"}
    for name, data in files.items():
        (workspace / name).write_bytes(data)
    before = {n: (workspace / n).stat() for n in files}
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    sh(session_name(workspace.resolve()), "echo x > /workspace/.env; rm -f /workspace/id_rsa")  # must fail
    assert main(["down", str(workspace)]) == 0
    for n, data in files.items():
        st = (workspace / n).stat()
        assert (workspace / n).read_bytes() == data
        assert (st.st_mtime_ns, st.st_ino, st.st_mode, st.st_nlink) == (
            before[n].st_mtime_ns, before[n].st_ino, before[n].st_mode, before[n].st_nlink)


def test_up_leaves_no_session_when_a_mask_cannot_be_applied(workspace, monkeypatch):
    """Force a mount failure: a Secret file that vanishes between planning and `up` must abort `up`."""
    from agent_sandbox import masks

    (workspace / ".env").write_text("TOKEN=abc\n")
    assert main(["build"]) == 0
    real = masks.check_originals_exist

    def vanish(plan):
        os.unlink(workspace / ".env")
        return real(plan)

    monkeypatch.setattr("agent_sandbox.runtime.check_originals_exist", vanish)
    assert main(["up", str(workspace)]) != 0
    ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={session_name(workspace.resolve())}"],
                        capture_output=True, text=True)
    assert ps.stdout.strip() == ""
    assert not (workspace / ".env").exists()  # docker must not have created a mount point inside the Workspace


GIT = "git -c user.name=asb -c user.email=asb@example.invalid"


def test_git_config_is_masked_and_local_git_still_works(workspace):
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://user:TOKEN@example.invalid/r.git"],
                   check=True)
    (workspace / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(workspace), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=h", "-c", "user.email=h@example.invalid",
                    "commit", "-qm", "first"], check=True)
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    try:
        assert sh(name, "wc -c < /workspace/.git/config").stdout.strip() == "0"
        assert "TOKEN" not in sh(name, "git -C /workspace remote -v; git -C /workspace config --list").stdout
        script = (
            "cd /workspace && git status --short && echo two >> a.txt && git diff --stat | grep a.txt"
            f" && git branch topic && {GIT} checkout -q topic && git add a.txt"
            f" && {GIT} commit -qm second && git log --oneline | wc -l | grep -qx 2"
        )
        r = sh(name, script)
        assert r.returncode == 0, r.stdout + r.stderr
        assert sh(name, "cd /workspace && git remote -v").stdout.strip() == ""  # no remotes inside the Session
    finally:
        main(["down", str(workspace)])
    log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 2  # the Session's local commit is on the host repo
    cfg = subprocess.run(["git", "-C", str(workspace), "remote", "-v"], capture_output=True, text=True).stdout
    assert "TOKEN" in cfg  # host config untouched


def test_submodule_config_is_masked(workspace):
    cfg = workspace / ".git" / "modules" / "sub" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[remote \"origin\"]\n\turl = https://u:TOKEN@example.invalid/s.git\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    try:
        assert sh(session_name(workspace.resolve()), "wc -c < /workspace/.git/modules/sub/config").stdout.strip() == "0"
    finally:
        main(["down", str(workspace)])


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


# ---------------------------------------------------------------------------- ticket 04 (aliases)
# UNVERIFIED: written on a host without a Docker daemon, so these have never run. See docs/spikes/0004-alias-masking.md.


@pytest.fixture
def alias_session(workspace):
    (workspace / ".env").write_text("TOKEN=abc\n")
    os.link(workspace / ".env", workspace / "notes.txt")                      # hard link
    (workspace / "docs").mkdir()
    os.link(workspace / ".env", workspace / "docs" / "harmless.md")           # hard link in a subdirectory
    (workspace / "config").mkdir()
    (workspace / "config" / "real.cfg").write_text("REAL=1\n")
    os.symlink("config/real.cfg", workspace / "secrets.json")                 # secret-named symlink, relative
    (workspace / "store").mkdir()
    (workspace / "store" / "k").write_text("KEY\n")
    (workspace / "app").mkdir()
    os.symlink("../store/k", workspace / "app" / ".env")                      # symlink into a sibling directory
    outside = workspace.parent / "outside-secret"
    outside.write_text("OUTSIDE\n")
    os.symlink(outside, workspace / "credentials.json")                       # points out: never followed
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_content_is_unreadable_through_every_alias(alias_session):
    for rel in (".env", "notes.txt", "docs/harmless.md", "config/real.cfg", "store/k"):
        r = sh(alias_session, f"wc -c < /workspace/{rel}")
        assert r.returncode == 0 and r.stdout.strip() == "0", rel
    for rel in ("secrets.json", "app/.env"):  # reading through the symlink reaches the masked real file
        assert sh(alias_session, f"cat /workspace/{rel}").stdout == "", rel


def test_secret_content_appears_nowhere_in_the_session_workspace(alias_session):
    r = sh(alias_session, "grep -rIl -e TOKEN=abc -e REAL=1 -e KEY /workspace 2>/dev/null; true")
    assert r.stdout.strip() == ""


def test_aliases_cannot_be_written_or_replaced(alias_session):
    for rel in ("notes.txt", "docs/harmless.md", "config/real.cfg", "store/k"):
        assert sh(alias_session, f"echo x > /workspace/{rel}").returncode != 0, rel
        assert sh(alias_session, f"rm -f /workspace/{rel}").returncode != 0, rel


def test_symlink_pointing_outside_dangles_in_the_session_and_nothing_outside_is_mounted(alias_session):
    assert sh(alias_session, "cat /workspace/credentials.json").returncode != 0
    assert "outside-secret" not in sh(alias_session, "cat /proc/self/mountinfo").stdout


def test_attach_warns_about_a_secret_file_created_after_up(alias_session, workspace, capsys):
    (workspace / "late").mkdir()
    (workspace / "late" / ".env.local").write_text("LATE=1\n")
    import unittest.mock as mock  # `attach` opens an interactive shell after warning; stub only that
    from agent_sandbox.runtime import Docker
    with mock.patch.object(Docker, "attach", return_value=0):
        assert main(["attach", str(workspace)]) == 0
    assert "late/.env.local" in capsys.readouterr().err
    assert sh(alias_session, "cat /workspace/late/.env.local").stdout == "LATE=1\n"  # the documented limit


def test_up_refuses_when_a_masked_path_became_a_symlinked_directory(workspace, monkeypatch):
    (workspace / "config").mkdir()
    (workspace / "config" / "real.env").write_text("S=1\n")
    os.symlink("config/real.env", workspace / ".env")
    assert main(["build"]) == 0
    from agent_sandbox import masks
    real = masks.check_originals_exist

    def swap(plan):
        os.rename(workspace / "config", workspace / "config2")
        os.symlink("config2", workspace / "config")
        return real(plan)

    monkeypatch.setattr("agent_sandbox.runtime.check_originals_exist", swap)
    assert main(["up", str(workspace)]) != 0
    ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={session_name(workspace.resolve())}"],
                        capture_output=True, text=True)
    assert ps.stdout.strip() == ""


# --------------------------------------------------------------------------- Host Services (ticket #6)
# UNVERIFIED: written on a host with no Docker daemon. See docs/spikes/host-services.md.


@pytest.fixture
def host_service_session(workspace, host_listener):
    """A Session whose config lists host.docker.internal:<host_listener> as `listed`. Nothing enabled yet."""
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nnetwork:\n  host_services:\n"
        f"    - {{name: listed, host: host.docker.internal, port: {host_listener}}}\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    yield session_name(workspace.resolve())
    main(["down", str(workspace)])


@pytest.fixture
def other_host_port():
    """A second listener on the host: the 'unlisted port on the same host'."""
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


def test_a_configured_host_service_is_closed_until_it_is_enabled(host_service_session, host_listener):
    assert not reaches(host_service_session, "host.docker.internal", host_listener)  # `up` opens nothing (ADR-0013)


def test_a_listed_host_port_is_reachable_and_an_unlisted_port_on_the_same_host_is_not(
        host_service_session, workspace, host_listener, other_host_port):
    assert main(["service", "enable", "listed", str(workspace)]) == 0
    assert reaches(host_service_session, "host.docker.internal", host_listener)
    assert not reaches(host_service_session, "host.docker.internal", other_host_port)
    assert not reaches(host_service_session, default_gateway(host_service_session), other_host_port)  # nor by address
    lan = host_lan_address()
    if lan:
        assert not reaches(host_service_session, lan, other_host_port)


def test_enabling_does_not_restart_the_session_or_netguard(host_service_session, workspace):
    def started(name):
        return subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}|{{.Id}}", name],
                              capture_output=True, text=True).stdout.strip()

    before = (started(host_service_session), started(netguard_of(host_service_session)))
    assert main(["service", "enable", "listed", str(workspace)]) == 0
    assert (started(host_service_session), started(netguard_of(host_service_session))) == before


def test_disable_closes_the_host_service_again(host_service_session, workspace, host_listener):
    assert main(["service", "enable", "listed", str(workspace)]) == 0
    assert reaches(host_service_session, "host.docker.internal", host_listener)
    assert main(["service", "disable", "listed", str(workspace)]) == 0
    assert not reaches(host_service_session, "host.docker.internal", host_listener)


def test_the_host_service_rules_end_with_the_session(host_service_session, workspace, host_listener):
    assert main(["service", "enable", "listed", str(workspace)]) == 0
    assert main(["down", str(workspace)]) == 0
    assert main(["up", str(workspace)]) == 0  # a new Netguard: nothing is enabled
    assert not reaches(host_service_session, "host.docker.internal", host_listener)


def test_the_session_cannot_open_a_host_service_itself(host_service_session, host_listener):
    r = sh(host_service_session, f"iptables -A NG_SERVICES -p tcp --dport {host_listener} -j ACCEPT")
    assert r.returncode != 0  # no capabilities, and the Session image has no iptables
    assert not reaches(host_service_session, "host.docker.internal", host_listener)


def test_doctor_reports_that_the_enabled_host_service_answers(host_service_session, workspace, capsys):
    assert main(["service", "enable", "listed", str(workspace)]) == 0
    capsys.readouterr()
    main(["doctor", str(workspace)])
    out = capsys.readouterr().out
    assert "Host Service listed" in out and "answers from the Session" in out
