"""Seam 1: `asb portal ...`, `up`, `down` and `ls` with Portals, against the fake docker (ADR-0013, ADR-0018)."""
import sys

import pytest

from agent_sandbox import portal_runtime, runtime
from agent_sandbox.cli import main
from agent_sandbox.plan import session_name

from conftest import FakeDocker
from pathlib import Path

PORTAL_FAKE = Path(__file__).with_name("fake_docker_portals.py")


class PortalDocker(FakeDocker):
    def __init__(self, state_file):
        super().__init__(state_file)
        self.command = [sys.executable, str(PORTAL_FAKE)]

    def state(self):
        return self._state()

    def rules(self, container):
        return {k.split("/", 1)[1]: v for k, v in self._state().get("iptables", {}).items() if k.startswith(container + "/")}


@pytest.fixture
def pdocker(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state.json"))
    monkeypatch.setattr(runtime, "READY_TIMEOUT", 0.3)
    monkeypatch.setattr(runtime, "READY_POLL", 0.01)
    monkeypatch.setattr(portal_runtime, "READY_TIMEOUT", 0.2)
    monkeypatch.setattr(portal_runtime, "READY_POLL", 0.01)
    d = PortalDocker(tmp_path / "docker-state.json")
    d.has_image("agent-sandbox-agent:latest", "agent-sandbox-netguard:latest", "agent-sandbox-portal:latest")
    return d


def run(argv, d, capsys):
    code = main(argv, docker=d.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def names(ws):
    s = session_name(ws.resolve())
    return s, f"{s}-netguard", f"{s}-portal-plannotator", f"{s}-portal-plannotator-net"


@pytest.fixture
def up(workspace, pdocker, capsys):
    assert run(["up", str(workspace)], pdocker, capsys)[0] == 0
    return workspace


def start(ws, d, capsys, name="plannotator"):
    return run(["portal", "start", name, str(ws)], d, capsys)


def calls(d, *prefix):
    return [c for c in d.calls if c[: len(prefix)] == list(prefix)]


def test_up_publishes_nothing_and_lists_the_portals_as_available(workspace, pdocker, capsys):
    code, out, _ = run(["up", str(workspace)], pdocker, capsys)
    assert code == 0
    assert not any("--publish" in c or c[:2] == ["network", "create"] for c in pdocker.calls)
    assert "plannotator" in out and "asb portal start" in out  # available, not active


def test_start_prints_name_arrow_url_on_loopback(up, pdocker, capsys):
    code, out, _ = start(up, pdocker, capsys)
    assert code == 0
    line = next(l for l in out.splitlines() if l.startswith("plannotator"))
    assert line.startswith("plannotator → http://127.0.0.1:") and line.endswith("/")


def test_start_publishes_on_loopback_with_a_docker_chosen_port(up, pdocker, capsys):
    start(up, pdocker, capsys)
    _, _, publisher, _ = names(up)
    args = pdocker.containers[publisher]["args"]
    assert args[args.index("--publish") + 1] == "127.0.0.1::8000"
    for a in pdocker.calls:  # nothing ever publishes on all interfaces
        assert not any(x.startswith(("0.0.0.0", "::")) for x in a if ":" in x and "--publish" in a)


def test_start_does_not_restart_or_replace_the_session_or_netguard(up, pdocker, capsys):
    session, guard, *_ = names(up)
    n = len(pdocker.calls)
    start(up, pdocker, capsys)
    after = pdocker.calls[n:]
    runs = [c for c in after if c[:1] == ["run"]]
    assert [c[c.index("--name") + 1] for c in runs] == [names(up)[2]]  # only the publisher is new
    for c in after:  # nothing is removed, restarted or stopped except the Portal's own leftovers
        if c[:1] in (["rm"], ["restart"], ["stop"], ["kill"]):
            assert c[-1] == names(up)[2]
    assert session in pdocker.containers and guard in pdocker.containers


def test_the_session_container_publishes_nothing_and_joins_no_extra_network(up, pdocker, capsys):
    start(up, pdocker, capsys)
    session, guard, publisher, net = names(up)
    assert "--publish" not in pdocker.containers[session]["args"]
    assert "--publish" not in pdocker.containers[guard]["args"]
    for c in calls(pdocker, "network", "connect"):
        assert c[-1] in (guard, publisher) and c[-1] != session


def test_netguard_admits_only_the_publisher_to_the_portal_port_and_drops_the_network_for_the_session(up, pdocker, capsys):
    start(up, pdocker, capsys)
    _, guard, publisher, net = names(up)
    r = pdocker.rules(guard)
    pub_ip = f"172.30.1.{2 + pdocker.state()['networks'][net]['members'].index(publisher)}"
    assert r["INPUT"] == [f"-p tcp -s {pub_ip} --dport 19432 -m comment --comment asb-portal:plannotator -j ACCEPT"]
    assert r["NG_LOCAL"] == ["-d 172.30.1.0/24 -j DROP"]
    assert not any("0.0.0.0" in x or "-s 0" in x for x in r["INPUT"])


def test_the_portal_network_is_internal(up, pdocker, capsys):
    start(up, pdocker, capsys)
    assert pdocker.state()["networks"][names(up)[3]]["internal"] is True


def test_start_is_idempotent_and_keeps_the_url(up, pdocker, capsys):
    _, out1, _ = start(up, pdocker, capsys)
    publishers = len(calls(pdocker, "run"))
    _, out2, _ = start(up, pdocker, capsys)
    assert out1.strip().splitlines()[-1] == out2.strip().splitlines()[-1]
    assert len(calls(pdocker, "run")) == publishers


def test_start_without_a_running_session_says_to_run_up(workspace, pdocker, capsys):
    code, _, err = start(workspace, pdocker, capsys)
    assert code != 0 and "asb up" in err and not calls(pdocker, "network", "create")


def test_start_unknown_portal_lists_the_configured_ones(up, pdocker, capsys):
    code, _, err = start(up, pdocker, capsys, name="nope")
    assert code != 0 and "unknown Portal 'nope'" in err and "plannotator" in err


def test_start_in_an_offline_session_is_refused(workspace, pdocker, capsys):
    run(["up", "--offline", str(workspace)], pdocker, capsys)
    code, _, err = start(workspace, pdocker, capsys)
    assert code != 0 and "offline" in err and not calls(pdocker, "network", "create")


def test_start_fails_when_the_portal_image_is_missing(workspace, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "s.json"))
    d = PortalDocker(tmp_path / "s.json")
    d.has_image("agent-sandbox-agent:latest", "agent-sandbox-netguard:latest")
    run(["up", str(workspace)], d, capsys)
    code, _, err = start(workspace, d, capsys)
    assert code != 0 and "asb build" in err and not calls(d, "network", "create")


def test_a_non_loopback_binding_is_refused_and_everything_is_undone(up, pdocker, capsys):
    pdocker.set(publish_on="0.0.0.0")
    code, out, err = start(up, pdocker, capsys)
    session, guard, publisher, net = names(up)
    assert code != 0 and "127.0.0.1" in err and "http://" not in out
    assert publisher not in pdocker.containers and net not in pdocker.state()["networks"]
    assert pdocker.rules(guard).get("INPUT", []) == [] and pdocker.rules(guard).get("NG_LOCAL", []) == []
    assert session in pdocker.containers and guard in pdocker.containers


def test_a_failed_firewall_rule_leaves_nothing_published(up, pdocker, capsys):
    pdocker.set(iptables_fails=True)
    code, out, err = start(up, pdocker, capsys)
    session, guard, publisher, net = names(up)
    assert code != 0 and "http://" not in out
    assert publisher not in pdocker.containers and net not in pdocker.state()["networks"]
    assert session in pdocker.containers and guard in pdocker.containers


def test_a_publisher_that_will_not_start_leaves_no_network_behind(up, pdocker, capsys):
    pdocker.set(fail_run=[names(up)[2]])
    code, _, err = start(up, pdocker, capsys)
    assert code != 0 and names(up)[3] not in pdocker.state()["networks"]


def test_two_sessions_get_different_host_ports(home, pdocker, capsys):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    run(["up", str(a)], pdocker, capsys)
    run(["up", str(b)], pdocker, capsys)
    urls = []
    for ws in (a, b):
        _, out, _ = start(ws, pdocker, capsys)
        urls.append(next(l for l in out.splitlines() if l.startswith("plannotator")).split("→")[1].strip())
    assert urls[0] != urls[1]
    assert names(a)[2] != names(b)[2] and names(a)[3] != names(b)[3]


def test_stop_unpublishes_and_keeps_the_session(up, pdocker, capsys):
    start(up, pdocker, capsys)
    code, out, _ = run(["portal", "stop", "plannotator", str(up)], pdocker, capsys)
    session, guard, publisher, net = names(up)
    assert code == 0
    assert publisher not in pdocker.containers and net not in pdocker.state()["networks"]
    assert pdocker.rules(guard)["INPUT"] == [] and pdocker.rules(guard)["NG_LOCAL"] == []
    assert session in pdocker.containers and guard in pdocker.containers


def test_stop_removes_input_rule_when_publisher_already_exited(up, pdocker, capsys):
    start(up, pdocker, capsys)
    _, guard, publisher, net = names(up)
    pdocker.kill(publisher)
    code, _, _ = run(["portal", "stop", "plannotator", str(up)], pdocker, capsys)
    assert code == 0
    assert publisher not in pdocker.containers and net not in pdocker.state()["networks"]
    assert pdocker.rules(guard)["INPUT"] == [] and pdocker.rules(guard)["NG_LOCAL"] == []


def test_network_ips_raises_portal_error_when_ip_address_is_empty(tmp_path):
    from agent_sandbox.portals import PortalError
    from agent_sandbox.runtime import Result

    class StubDocker:
        def run(self, *args, **kwargs):
            return Result(0, '{"net": {"IPAddress": ""}}', "")

    with pytest.raises(PortalError, match="no address"):
        portal_runtime._network_ips(StubDocker(), "ctr", "net")


def test_stop_never_touches_a_service_the_agent_started(up, pdocker, capsys):
    start(up, pdocker, capsys)
    session = names(up)[0]
    before = len(calls(pdocker, "exec", session))
    run(["portal", "stop", "plannotator", str(up)], pdocker, capsys)
    assert not [c for c in calls(pdocker, "exec", session)[before:] if "sh" in c]


def test_stop_when_not_published_is_fine(up, pdocker, capsys):
    code, out, _ = run(["portal", "stop", "plannotator", str(up)], pdocker, capsys)
    assert code == 0 and "not published" in out


def test_restart_republishes(up, pdocker, capsys):
    start(up, pdocker, capsys)
    code, out, _ = run(["portal", "restart", "plannotator", str(up)], pdocker, capsys)
    assert code == 0 and "plannotator → http://127.0.0.1:" in out
    assert names(up)[2] in pdocker.containers


def test_status_reports_publication_and_service_separately(up, pdocker, capsys):
    _, out, _ = run(["portal", "status", "plannotator", str(up)], pdocker, capsys)
    assert "not published" in out
    pdocker.set(portal_listening=False)
    start(up, pdocker, capsys)  # an agent-started Portal: publication does not wait for the service
    _, out, _ = run(["portal", "status", "plannotator", str(up)], pdocker, capsys)
    assert "published at http://127.0.0.1:" in out and "not listening" in out
    assert "does not prove" in out and "agent" in out
    pdocker.set(portal_listening=True)
    _, out, _ = run(["portal", "status", "plannotator", str(up)], pdocker, capsys)
    assert "service:     listening" in out


CMD_PORTAL = "portals: {docs: {port: 8080, command: [serve, '8080'], env: {DOCS_MODE: x}}}\n"


@pytest.fixture
def cmd_ws(workspace, pdocker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\n" + CMD_PORTAL)
    run(["up", str(workspace)], pdocker, capsys)
    return workspace


def test_a_portal_with_a_command_runs_it_in_the_session_with_its_env_then_publishes(cmd_ws, pdocker, capsys):
    code, out, _ = start(cmd_ws, pdocker, capsys, name="docs")
    session = names(cmd_ws)[0]
    assert code == 0 and "docs → http://127.0.0.1:" in out
    ex = [c for c in calls(pdocker, "exec", "-d") if session in c][0]
    assert ex[ex.index("-e") + 1] == "DOCS_MODE=x" and ex[-2:] == ["serve", "8080"]
    first_exec = pdocker.calls.index(ex)
    assert first_exec < pdocker.calls.index(calls(pdocker, "network", "create")[0])  # the service comes first


def test_stop_stops_the_process_the_cli_started(cmd_ws, pdocker, capsys):
    start(cmd_ws, pdocker, capsys, name="docs")
    _, out, _ = run(["portal", "status", "docs", str(cmd_ws)], pdocker, capsys)
    assert "running" in out
    run(["portal", "stop", "docs", str(cmd_ws)], pdocker, capsys)
    _, out, _ = run(["portal", "status", "docs", str(cmd_ws)], pdocker, capsys)
    assert "not published" in out and "not running" in out


def test_a_command_that_never_listens_fails_and_publishes_nothing(cmd_ws, pdocker, capsys):
    pdocker.set(portal_listening=False)
    code, out, err = start(cmd_ws, pdocker, capsys, name="docs")
    assert code != 0 and "did not listen" in err and "http://" not in out
    assert not calls(pdocker, "network", "create")
    _, st, _ = run(["portal", "status", "docs", str(cmd_ws)], pdocker, capsys)
    assert "not running" in st  # the process was stopped again


def test_a_command_that_cannot_start_fails_and_publishes_nothing(cmd_ws, pdocker, capsys):
    pdocker.set(process_dies=True)
    code, out, err = start(cmd_ws, pdocker, capsys, name="docs")
    assert code != 0 and not calls(pdocker, "network", "create")


def test_down_removes_publishers_and_portal_networks_so_host_ports_are_released(up, pdocker, capsys):
    start(up, pdocker, capsys)
    code, _, _ = run(["down", str(up)], pdocker, capsys)
    assert code == 0 and pdocker.containers == {} and pdocker.state()["networks"] == {}


def test_up_after_down_starts_with_every_portal_off(up, pdocker, capsys):
    start(up, pdocker, capsys)
    run(["down", str(up)], pdocker, capsys)
    run(["up", str(up)], pdocker, capsys)
    assert names(up)[2] not in pdocker.containers
    _, out, _ = run(["ls"], pdocker, capsys)
    assert "http://" not in out


def test_up_cleans_leftover_portal_networks_of_a_crashed_session(up, pdocker, capsys):
    start(up, pdocker, capsys)
    for n in (names(up)[0], names(up)[1]):  # the Session and Netguard died; the publisher and network stayed
        s = pdocker.state()
        del s["containers"][n]
        pdocker._write(s)
    code, _, _ = run(["up", str(up)], pdocker, capsys)
    assert code == 0 and names(up)[2] not in pdocker.containers and pdocker.state()["networks"] == {}


def test_ls_lists_the_urls_of_published_portals_only(up, pdocker, capsys):
    _, out, _ = run(["ls"], pdocker, capsys)
    assert str(up.resolve()) in out and "http://" not in out
    _, started, _ = start(up, pdocker, capsys)
    url = next(l for l in started.splitlines() if l.startswith("plannotator")).split("→")[1].strip()
    _, out, _ = run(["ls"], pdocker, capsys)
    assert f"plannotator → {url}" in out and str(up.resolve()) in out


def test_check_lists_the_configured_portals_as_not_started(workspace, pdocker, capsys):
    code, out, _ = run(["check", str(workspace)], pdocker, capsys)
    assert code == 0 and "Portals:" in out and "plannotator" in out and "127.0.0.1" in out and "19432" in out
    assert not pdocker.calls  # check never touches Docker


def test_check_shows_a_project_portal_command(workspace, pdocker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\n" + CMD_PORTAL)
    _, out, _ = run(["check", str(workspace)], pdocker, capsys)
    assert "Project config: portals.docs.command serve 8080" in out


def test_build_builds_the_portal_image_and_doctor_checks_it(pdocker, tmp_path, monkeypatch, capsys):
    s = tmp_path / "b.json"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(s))
    d = PortalDocker(s)
    code, _, _ = run(["build"], d, capsys)
    assert code == 0 and "agent-sandbox-portal:latest" in d.state()["images"]
    code, out, _ = run(["doctor"], d, capsys)
    assert "agent-sandbox-portal:latest" in out and code == 0


def test_the_session_is_walled_off_from_the_portal_network_before_the_publisher_exists(up, pdocker, capsys):
    n = len(pdocker.calls)
    start(up, pdocker, capsys)
    after = pdocker.calls[n:]
    drop = next(i for i, c in enumerate(after) if "iptables" in c and "NG_LOCAL" in c and "-A" in c)
    publisher_run = next(i for i, c in enumerate(after) if c[:1] == ["run"])
    admit = next(i for i, c in enumerate(after) if "iptables" in c and "INPUT" in c and "-A" in c)
    assert drop < publisher_run < admit
