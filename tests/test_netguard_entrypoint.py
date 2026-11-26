"""Seam 1: run the real Netguard entrypoint script against stub `iptables*` and `ip` binaries.

Proves the fail-closed behaviour of the script itself: it signals ready only after the rules are
applied and verified, and it exits non-zero without a ready file in every failure case. No Docker.
What the kernel then enforces is proved by the Seam 2 tests.
"""
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_sandbox.netguard import BLOCKED_RANGES, RULES_ENV, rules

ENTRYPOINT = Path(__file__).parents[1] / "src" / "agent_sandbox" / "images" / "netguard" / "entrypoint.sh"
DOCKERFILE = ENTRYPOINT.with_name("Dockerfile")

STUB_RESTORE = '#!/bin/sh\n[ -z "$STUB_RESTORE_FAILS" ] || { echo "restore failed" >&2; exit 1; }\ncat > "$STUB_DIR/applied"\n'
STUB_IPTABLES = '#!/bin/sh\n[ -z "$STUB_IPTABLES_FAILS" ] || exit 1\nprintf \'%s\\n\' "$*" >> "$STUB_DIR/appended"\n'
STUB_SAVE = '#!/bin/sh\ncat "$STUB_DIR/applied" || exit 1\n[ ! -f "$STUB_DIR/appended" ] || cat "$STUB_DIR/appended"\n'
STUB_IP = """#!/bin/sh
case "$*" in
  *route*) echo "default via ${STUB_GATEWAY:-172.17.0.1} dev eth0" ;;
  *addr*) echo "2: eth0    inet ${STUB_ADDR:-172.17.0.2/16} brd 172.17.255.255 scope global eth0" ;;
esac
"""


@pytest.fixture
def env(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("iptables-restore", STUB_RESTORE), ("iptables", STUB_IPTABLES),
                       ("iptables-save", STUB_SAVE), ("ip", STUB_IP)):
        f = stubs / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 192.168.1.1\nnameserver 8.8.8.8\nnameserver 2001:4860:4860::8888\nsearch lan\n")
    ipv6 = tmp_path / "disable_ipv6"
    ipv6.write_text("1\n")
    run_dir = tmp_path / "run"
    return {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_DIR": str(tmp_path),
        RULES_ENV: ";".join(rules()),
        "ASB_NETGUARD_RESOLV_CONF": str(resolv),
        "ASB_NETGUARD_IPV6_FLAG": str(ipv6),
        "ASB_NETGUARD_RUN_DIR": str(run_dir),
    }


def start(env):
    return subprocess.Popen(["sh", str(ENTRYPOINT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(p, ready_file, wait=3.0):
    """Wait until the script is ready or has exited. Returns (exit code or None if idling, stderr)."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if ready_file.exists() or p.poll() is not None:
            break
        time.sleep(0.02)
    if p.poll() is None:
        p.terminate()
        p.wait(timeout=5)
        return None, ""
    return p.returncode, p.stderr.read()


def run(env, wait=3.0):
    ready = Path(env["ASB_NETGUARD_RUN_DIR"]) / "ready"
    p = start(env)
    try:
        code, err = finish(p, ready, wait)
    finally:
        if p.poll() is None:
            p.kill()
    return code, err, ready


def applied(env):
    return (Path(env["STUB_DIR"]) / "applied").read_text()


def appended(env):
    f = Path(env["STUB_DIR"]) / "appended"
    return f.read_text().splitlines() if f.exists() else []


def test_script_is_posix_sh_and_has_no_bashisms():
    first = ENTRYPOINT.read_text().splitlines()[0]
    assert first == "#!/bin/sh"
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
    env["PATH"] = "/usr/bin:/bin"  # stubs gone; real iptables-restore is not installed in the test host
    if subprocess.run(["sh", "-c", "command -v iptables-restore"], env=env, capture_output=True).returncode == 0:
        pytest.skip("host has a real iptables-restore")
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_no_ready_file_when_the_applied_rules_do_not_verify(env, tmp_path):
    # a restore that silently applies nothing must not be reported as ready
    (tmp_path / "bin" / "iptables-restore").write_text("#!/bin/sh\ncat > /dev/null\n")
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert "verify" in err.lower()


def test_no_ready_file_when_ipv6_is_not_disabled(env):
    Path(env["ASB_NETGUARD_IPV6_FLAG"]).write_text("0\n")
    code, err, ready = run(env)
    assert code not in (None, 0) and not ready.exists()
    assert "ipv6" in err.lower()


def test_no_ready_file_when_ipv6_state_cannot_be_read(env):
    Path(env["ASB_NETGUARD_IPV6_FLAG"]).unlink()
    code, _, ready = run(env)
    assert code not in (None, 0) and not ready.exists()


def test_every_blocked_range_is_checked_by_the_verification(env, tmp_path):
    # drop one blocked range from what the stub "applied": the script must notice
    (tmp_path / "bin" / "iptables-restore").write_text(
        "#!/bin/sh\ngrep -v -- '-d 100.64.0.0/10 ' > \"$STUB_DIR/applied\"\n")
    code, _, ready = run(env)
    assert "100.64.0.0/10" in BLOCKED_RANGES
    assert code not in (None, 0) and not ready.exists()


def test_netguard_dockerfile_is_alpine_with_iptables_and_the_entrypoint():
    text = DOCKERFILE.read_text()
    assert text.startswith("# ") or text.startswith("FROM alpine")
    assert "FROM alpine:" in text and "iptables" in text
    assert "entrypoint.sh" in text and "ENTRYPOINT" in text
    assert "USER" not in text.replace("# USER", "")  # needs root + NET_ADMIN to run iptables
