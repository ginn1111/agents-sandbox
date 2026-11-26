"""Seam 1: run the real Netguard `asb-service` helper against a stubbed, stateful `iptables`.

Proves the helper's own behaviour: what it adds to NG_SERVICES per Host Service, that it refuses to
open anything wrong, and that a failure leaves nothing open. What the kernel then enforces for a
listed and an unlisted port is proved by the Seam 2 tests (UNVERIFIED here: no Docker daemon).
Stubs are executable scripts, so this needs a directory that is not mounted noexec (see
test_netguard_entrypoint.py); without one these tests skip.
"""
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_netguard_entrypoint import EXEC_DIR  # tests/ is on sys.path (no package)

HELPER = Path(__file__).parents[1] / "src" / "agent_sandbox" / "images" / "netguard" / "service.sh"
DOCKERFILE = HELPER.with_name("Dockerfile")

# A tiny iptables for the NG_SERVICES chain: -A appends, -D deletes one exact line, -S lists with the
# normalisation the real tool applies (/32 on a host address, `-m tcp`, `-m comment`).
STUBS = {
    "iptables": r"""#!/bin/sh
chain_file="$STUB_DIR/chain"
[ -z "$STUB_IPTABLES_FAILS" ] || exit 1
touch "$chain_file"
norm() {
    printf '%s\n' "$*" | sed -e 's/ -d \([0-9.]*\) / -d \1\/32 /' -e 's/ --dport / -m tcp --dport /' -e 's/ -m tcp -m tcp/ -m tcp/' \
        -e 's/ --comment \([^ ]*\)/ -m comment --comment \1/' -e 's/ -m comment -m comment/ -m comment/'
}
case "$1" in
    -A) shift; if [ -n "$STUB_ADD_SILENTLY_DROPS" ]; then exit 0; fi
        norm "-A $@" >> "$chain_file" ;;
    -D) shift; line=$(norm "-A $@"); grep -vxF -- "$line" "$chain_file" > "$chain_file.new"; mv "$chain_file.new" "$chain_file" ;;
    -S) echo "-N NG_SERVICES"; cat "$chain_file" ;;
    *) exit 1 ;;
esac
""",
    "getent": """#!/bin/sh
[ "$1" = ahostsv4 ] || exit 2
case "$2" in
    host.docker.internal) echo "${STUB_HOST_ADDR:-172.17.0.1}  STREAM $2"; echo "${STUB_HOST_ADDR:-172.17.0.1}  DGRAM" ;;
    two.internal) echo "10.1.1.1  STREAM"; echo "10.1.1.2  STREAM" ;;
    loop.internal) echo "127.0.0.1  STREAM" ;;
    meta.internal) echo "169.254.169.254  STREAM" ;;
    *) exit 2 ;;
esac
""",
}

pytestmark = pytest.mark.skipif(EXEC_DIR is None, reason="no directory that allows executing stub scripts")


@pytest.fixture
def env(tmp_path):
    bin_dir = Path(tempfile.mkdtemp(dir=EXEC_DIR, prefix="asb-stubs-"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ready").write_text("")
    for name, body in STUBS.items():
        f = bin_dir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    yield {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_DIR": str(tmp_path),
        "ASB_NETGUARD_RUN_DIR": str(run_dir),
    }
    shutil.rmtree(bin_dir, ignore_errors=True)


def helper(env, *args):
    return subprocess.run(["sh", str(HELPER), *args], env=env, capture_output=True, text=True)


def chain(env):
    f = Path(env["STUB_DIR"]) / "chain"
    return f.read_text().splitlines() if f.exists() else []


def test_script_is_posix_sh():
    assert HELPER.read_text().splitlines()[0] == "#!/bin/sh"
    assert subprocess.run(["sh", "-n", str(HELPER)]).returncode == 0


def test_the_image_ships_the_helper_and_getent():
    text = DOCKERFILE.read_text()
    assert "service.sh" in text and "/usr/local/bin/asb-service" in text


def test_enable_adds_one_tcp_accept_rule_for_the_listed_ip_and_port(env):
    r = helper(env, "enable", "llm", "192.168.5.2", "20128")
    assert r.returncode == 0, r.stderr
    (line,) = chain(env)
    assert line.startswith("-A NG_SERVICES -d 192.168.5.2/32 -p tcp ")
    assert "--dport 20128" in line and "--dport 80" not in line
    assert line.endswith("-j ACCEPT") and "asb-svc:llm" in line


def test_a_name_is_resolved_to_every_ipv4_address_and_each_gets_a_rule(env):
    assert helper(env, "enable", "two", "two.internal", "80").returncode == 0
    ips = sorted(l.split(" -d ")[1].split("/")[0] for l in chain(env))
    assert ips == ["10.1.1.1", "10.1.1.2"]


def test_host_docker_internal_resolves_to_the_gateway_address(env):
    assert helper(env, "enable", "dbx", "host.docker.internal", "4224").returncode == 0
    assert "-d 172.17.0.1/32" in chain(env)[0]  # one address, though getent printed it twice


def test_enable_twice_does_not_duplicate_rules(env):
    helper(env, "enable", "dbx", "host.docker.internal", "4224")
    helper(env, "enable", "dbx", "host.docker.internal", "4224")
    assert len(chain(env)) == 1


def test_enable_again_with_a_new_port_replaces_the_old_rule(env):
    helper(env, "enable", "dbx", "192.168.5.2", "1000")
    helper(env, "enable", "dbx", "192.168.5.2", "2000")
    (line,) = chain(env)
    assert "--dport 2000" in line


def test_disable_removes_only_that_services_rules(env):
    helper(env, "enable", "a", "192.168.5.2", "1000")
    helper(env, "enable", "b", "192.168.5.3", "2000")
    assert helper(env, "disable", "a").returncode == 0
    (line,) = chain(env)
    assert "asb-svc:b" in line


def test_disable_of_a_closed_service_succeeds(env):
    assert helper(env, "disable", "never-enabled").returncode == 0 and chain(env) == []


def test_list_prints_the_enabled_service_names_once_each(env):
    helper(env, "enable", "two", "two.internal", "80")
    helper(env, "enable", "b", "192.168.5.3", "2000")
    r = helper(env, "list")
    assert r.returncode == 0 and sorted(r.stdout.split()) == ["b", "two"]


@pytest.mark.parametrize("args", [
    ("enable", "bad name", "192.168.5.2", "80"),
    ("enable", "a;b", "192.168.5.2", "80"),
    ("enable", "a", "192.168.5.2", "0"),
    ("enable", "a", "192.168.5.2", "65536"),
    ("enable", "a", "192.168.5.2", "80; reboot"),
    ("enable", "a", "192.168.5.2/16", "80"),          # a network is not a host
    ("enable", "a", "-s", "80"),
    ("enable", "a", "127.0.0.1", "80"),
    ("enable", "a", "0.0.0.0", "80"),
    ("enable", "a", "169.254.169.254", "80"),
    ("enable", "a", "224.0.0.1", "80"),
    ("enable", "a", "loop.internal", "80"),           # resolves to loopback
    ("enable", "a", "meta.internal", "80"),           # resolves to the cloud metadata address
    ("enable", "a", "unknown.internal", "80"),        # does not resolve
    ("enable", "a", "192.168.5.2"),
    ("enable",),
    ("frobnicate",),
    (),
])
def test_bad_input_opens_nothing_and_exits_non_zero(env, args):
    r = helper(env, *args)
    assert r.returncode != 0 and chain(env) == []


def test_enable_refuses_before_netguard_is_ready(env):
    (Path(env["ASB_NETGUARD_RUN_DIR"]) / "ready").unlink()
    r = helper(env, "enable", "a", "192.168.5.2", "80")
    assert r.returncode != 0 and chain(env) == [] and "ready" in r.stderr


def test_a_failing_iptables_fails_the_command_and_leaves_nothing_open(env):
    env["STUB_IPTABLES_FAILS"] = "1"
    r = helper(env, "enable", "a", "192.168.5.2", "80")
    assert r.returncode != 0 and chain(env) == []


def test_a_rule_that_does_not_show_up_in_the_chain_is_an_error_and_is_rolled_back(env):
    env["STUB_ADD_SILENTLY_DROPS"] = "1"
    r = helper(env, "enable", "a", "192.168.5.2", "80")
    assert r.returncode != 0 and "verify" in r.stderr and chain(env) == []


def test_a_name_with_two_addresses_is_all_or_nothing(env):
    # the second add fails: the first rule must not stay behind
    stub = Path(env["PATH"].split(":")[0]) / "iptables"
    stub.write_text(stub.read_text().replace(
        "    -A) shift;", "    -A) shift; [ \"$(wc -l < \"$chain_file\")\" -lt 1 ] || exit 1;"))
    r = helper(env, "enable", "two", "two.internal", "80")
    assert r.returncode != 0 and chain(env) == []
