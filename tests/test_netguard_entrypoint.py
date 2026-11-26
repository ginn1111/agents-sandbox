"""Seam 1: run the real Netguard entrypoint script against stubbed `iptables*` and `ip` commands.

Proves the fail-closed behaviour of the script itself: it signals ready only after the rules are
applied and verified, and it exits non-zero without a ready file in every failure case. No Docker.
What the kernel then enforces is proved by the Seam 2 tests.

The stubs are executable scripts, so they need a directory that is not mounted noexec (often /tmp).
The first candidate that can execute is used; without one, these tests skip.
"""
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent_sandbox.netguard import BLOCKED_RANGES, RULES_ENV, rules

ENTRYPOINT = Path(__file__).parents[1] / "src" / "agent_sandbox" / "images" / "netguard" / "entrypoint.sh"
DOCKERFILE = ENTRYPOINT.with_name("Dockerfile")

STUBS = {
    "iptables-restore": """#!/bin/sh
[ -z "$STUB_RESTORE_FAILS" ] || { echo "restore failed" >&2; exit 1; }
cat > "$STUB_DIR/applied"
""",
    "iptables": """#!/bin/sh
[ -z "$STUB_IPTABLES_FAILS" ] || exit 1
printf '%s\\n' "$*" >> "$STUB_DIR/appended"
""",
    "iptables-save": """#!/bin/sh
cat "$STUB_DIR/applied" || exit 1
[ ! -f "$STUB_DIR/appended" ] || cat "$STUB_DIR/appended"
""",
    "ip": """#!/bin/sh
case "$*" in
    *route*) echo "default via ${STUB_GATEWAY:-172.17.0.1} dev eth0" ;;
    *addr*) echo "2: eth0    inet ${STUB_ADDR:-172.17.0.2/16} brd 172.17.255.255 scope global eth0" ;;
esac
""",
}


def _can_exec_in(directory):
    try:
        d = Path(tempfile.mkdtemp(dir=directory, prefix=".asb-exec-probe-"))
    except OSError:
        return None
    probe = d / "probe"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(0o755)
    try:
        ok = subprocess.run([str(probe)], capture_output=True).returncode == 0
    except OSError:
        ok = False
    shutil.rmtree(d, ignore_errors=True)
    return directory if ok else None


def _exec_dir():
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    for candidate in (tempfile.gettempdir(), "/dev/shm", "/var/tmp", real_home):
        if candidate and os.path.isdir(candidate) and _can_exec_in(candidate):
            return candidate
    return None


EXEC_DIR = _exec_dir()
pytestmark = pytest.mark.skipif(EXEC_DIR is None, reason="no directory that allows executing stub scripts")


@pytest.fixture
def env(tmp_path):
    bin_dir = Path(tempfile.mkdtemp(dir=EXEC_DIR, prefix="asb-stubs-"))
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 192.168.1.1\nnameserver 8.8.8.8\nnameserver 2001:4860:4860::8888\nsearch lan\n")
    ipv6 = tmp_path / "disable_ipv6"
    ipv6.write_text("1\n")
    yield {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_DIR": str(tmp_path),
        "STUB_BIN": str(bin_dir),
        RULES_ENV: ";".join(rules()),
        "ASB_NETGUARD_RESOLV_CONF": str(resolv),
        "ASB_NETGUARD_IPV6_FLAG": str(ipv6),
        "ASB_NETGUARD_RUN_DIR": str(tmp_path / "run"),
        "STUBS": dict(STUBS),  # name -> script body; tests replace or drop entries
    }
    shutil.rmtree(bin_dir, ignore_errors=True)


def run(env, wait=3.0, stop_at_ready=True):
    """Start the entrypoint, wait until it is ready or has exited. Returns (exit code or None, stderr, ready file)."""
    env = dict(env)
    for name, body in env.pop("STUBS").items():
        f = Path(env["STUB_BIN"]) / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    ready = Path(env["ASB_NETGUARD_RUN_DIR"]) / "ready"
    p = subprocess.Popen(["sh", str(ENTRYPOINT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + wait
        while time.time() < deadline and not (stop_at_ready and ready.exists()) and p.poll() is None:
            time.sleep(0.02)
        if p.poll() is None:  # ready and idling (or hung: the ready-file assertions catch that)
            p.terminate()
            p.wait(timeout=5)
            return None, "", ready
        return p.returncode, p.stderr.read(), ready
    finally:
        if p.poll() is None:
            p.kill()


def applied(env):
    return (Path(env["STUB_DIR"]) / "applied").read_text()


def appended(env):
    f = Path(env["STUB_DIR"]) / "appended"
    return f.read_text().splitlines() if f.exists() else []


def test_script_is_posix_sh():
    assert ENTRYPOINT.read_text().splitlines()[0] == "#!/bin/sh"
    assert subprocess.run(["sh", "-n", str(ENTRYPOINT)]).returncode == 0


def test_signals_ready_and_keeps_idling_after_applying_the_rules(env):
    code, _, ready = run(env)
    assert code is None and ready.exists()  # still running: Netguard idles


def test_rules_are_applied_exactly_as_generated(env):
    run(env)
    assert applied(env).splitlines() == rules()


def test_dns_is_opened_only_to_the_configured_ipv4_resolvers_on_port_53(env):
    run(env)
    dns = [l for l in appended(env) if "NG_DNS" in l]
    assert sorted(dns) == sorted(
        f"-A NG_DNS -d {ns} -p {proto} --dport 53 -j ACCEPT"
        for ns in ("192.168.1.1", "8.8.8.8") for proto in ("udp", "tcp"))


def test_own_gateway_and_subnet_are_dropped_even_if_docker_uses_a_public_range(env):
    env["STUB_GATEWAY"], env["STUB_ADDR"] = "203.0.113.1", "203.0.113.2/24"
    run(env)
    local = [l for l in appended(env) if "NG_LOCAL" in l]
    assert "-A NG_LOCAL -d 203.0.113.1 -j DROP" in local
    assert "-A NG_LOCAL -d 203.0.113.2/24 -j DROP" in local


def test_no_ready_file_and_nonzero_exit_when_the_rules_are_missing(env):
    del env[RULES_ENV]
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert RULES_ENV in err


def test_no_ready_file_when_iptables_restore_fails(env):
    env["STUB_RESTORE_FAILS"] = "1"
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_no_ready_file_when_adding_resolver_or_local_rules_fails(env):
    env["STUB_IPTABLES_FAILS"] = "1"
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_no_ready_file_when_a_required_tool_is_missing(env):
    del env["STUBS"]["iptables-restore"]
    if shutil.which("iptables-restore", path="/usr/bin:/bin"):
        pytest.skip("host has a real iptables-restore")
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert "iptables-restore" in err


def test_no_ready_file_when_the_applied_rules_do_not_verify(env):
    # a restore that silently applies nothing must not be reported as ready
    env["STUBS"]["iptables-restore"] = '#!/bin/sh\ncat > /dev/null\n: > "$STUB_DIR/applied"\n'
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert "verify" in err.lower()


def test_every_blocked_range_is_checked_by_the_verification(env):
    assert "100.64.0.0/10" in BLOCKED_RANGES
    env["STUBS"]["iptables-restore"] = "#!/bin/sh\ngrep -v -- '-d 100.64.0.0/10 ' > \"$STUB_DIR/applied\"\n"
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_no_ready_file_when_ipv6_is_not_disabled(env):
    Path(env["ASB_NETGUARD_IPV6_FLAG"]).write_text("0\n")
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert "ipv6" in err.lower()


def test_no_ready_file_when_ipv6_state_cannot_be_read(env):
    Path(env["ASB_NETGUARD_IPV6_FLAG"]).unlink()
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_stale_ready_file_is_removed_on_start(env):
    ready = Path(env["ASB_NETGUARD_RUN_DIR"]) / "ready"
    ready.parent.mkdir(parents=True)
    ready.write_text("")
    env["STUB_RESTORE_FAILS"] = "1"
    code, _, _ = run(env, stop_at_ready=False)  # the stale file must not count as ready
    assert code not in (None, 0) and not ready.exists()


def test_netguard_dockerfile_is_alpine_with_iptables_and_the_entrypoint():
    lines = DOCKERFILE.read_text().splitlines()
    text = "\n".join(lines)
    assert "FROM alpine:" in text and "iptables" in text
    assert "entrypoint.sh" in text and "ENTRYPOINT" in text
    assert not any(l.startswith("USER") for l in lines)  # needs root + NET_ADMIN to run iptables
