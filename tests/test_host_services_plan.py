"""Seam 1: how the plan, `check` and the explicit `service` commands treat Host Services. No Docker."""
import json

import pytest

from agent_sandbox import host_services
from agent_sandbox.cli import main
from agent_sandbox.config import load_config
from agent_sandbox.netguard import rules
from agent_sandbox.plan import plan, session_name

HELPER = "/usr/local/bin/asb-service"
CONFIG = """\
version: 1
network:
  host_services:
    - {name: dbx-web, host: host.docker.internal, port: 4224}
    - {name: llm-gateway, host: 192.168.5.2, port: 20128}
"""


def make_plan(ws, **cli):
    return plan(ws, load_config(ws, cli=cli or None))


def flag_values(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def configured(workspace):
    (workspace / ".agent-sandbox.yaml").write_text(CONFIG)
    return workspace


@pytest.fixture
def up(configured, docker, capsys):
    docker.has_image()
    assert run(["up", str(configured)], docker, capsys)[0] == 0
    return configured


def helper_calls(docker):
    return [c for c in docker.calls if c[:1] == ["exec"] and HELPER in c]


# -- plan -------------------------------------------------------------------------------------------


def test_the_plan_lists_each_host_service_with_the_rule_netguard_will_add(configured):
    svcs = {s["name"]: s for s in make_plan(configured).host_services}
    assert sorted(svcs) == ["dbx-web", "llm-gateway"]
    assert svcs["llm-gateway"]["host"] == "192.168.5.2" and svcs["llm-gateway"]["port"] == 20128
    assert svcs["llm-gateway"]["rule"] == "-A NG_SERVICES -d 192.168.5.2 -p tcp --dport 20128 -j ACCEPT"
    assert svcs["dbx-web"]["rule"] == "-A NG_SERVICES -d host.docker.internal -p tcp --dport 4224 -j ACCEPT"


def test_no_rule_for_any_host_service_is_part_of_the_rules_netguard_starts_with(configured):
    p = make_plan(configured)
    assert p.netguard_rules == rules()
    assert not [l for l in p.netguard_rules if "--dport" in l]
    env = [e for e in flag_values(p.netguard_run, "--env") if e.startswith("ASB_NETGUARD_RULES=")][0]
    assert "4224" not in env and "20128" not in env  # `up` opens nothing (ADR-0013)
    assert all(not s["active"] for s in p.host_services)


def test_each_rule_opens_one_host_and_one_tcp_port_only(configured):
    for s in make_plan(configured).host_services:
        assert s["rule"].count("--dport") == 1 and "-p tcp" in s["rule"]
        assert s["rule"].endswith("-j ACCEPT") and "0.0.0.0" not in s["rule"] and "/" not in s["rule"].split("-d ")[1].split()[0]


def test_the_host_gateway_name_is_added_on_netguard_not_the_session(configured):
    p = plan(configured, load_config(configured), platform="linux")
    assert flag_values(p.netguard_run, "--add-host") == ["host.docker.internal:host-gateway"]
    assert "--add-host" not in p.session_run


def test_no_host_service_means_an_empty_list(workspace):
    assert make_plan(workspace).host_services == []


def test_offline_plans_have_no_host_services_and_say_why(configured):
    p = make_plan(configured, offline=True)
    assert p.host_services == []
    assert any("offline" in w and "Host Service" in w for w in p.warnings)


def test_enable_commands_carry_no_host_environment_or_credentials(configured):
    for s in make_plan(configured).host_services:
        argv = s["enable"]
        assert argv[:3] == ["exec", session_name(configured.resolve()) + "-netguard", HELPER]
        assert "--env" not in argv and "-e" not in argv and "--privileged" not in argv


# -- check ------------------------------------------------------------------------------------------


def test_check_lists_host_services_and_says_they_are_off_until_enabled(configured, docker, capsys):
    code, out, _ = run(["check", str(configured)], docker, capsys)
    assert code == 0 and docker.calls == []
    assert "Host Services:" in out
    assert "dbx-web host.docker.internal:4224" in out and "llm-gateway 192.168.5.2:20128" in out
    assert "asb service enable dbx-web" in out


def test_check_says_so_when_no_host_service_is_configured(workspace, docker, capsys):
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert "Host Services:  none" in out


def test_check_json_carries_the_host_services(configured, docker, capsys):
    _, out, _ = run(["check", "--json", str(configured)], docker, capsys)
    assert [s["name"] for s in json.loads(out)["host_services"]] == ["dbx-web", "llm-gateway"]


def test_check_flags_host_services_that_come_from_project_config(configured, docker, capsys):
    _, out, _ = run(["check", str(configured)], docker, capsys)
    assert "Project config: network.host_services dbx-web host.docker.internal:4224" in out


# -- up never enables -------------------------------------------------------------------------------


def test_up_does_not_open_any_host_service(up, docker):
    assert helper_calls(docker) == []


# -- service enable / disable -----------------------------------------------------------------------


def test_service_enable_runs_the_helper_in_netguard_with_that_one_service(up, docker, capsys):
    code, out, _ = run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    guard = session_name(up.resolve()) + "-netguard"
    assert code == 0 and "dbx-web" in out
    assert helper_calls(docker) == [["exec", guard, HELPER, "enable", "dbx-web", "host.docker.internal", "4224"]]
    assert [c for c in docker.calls if c[0] == "run"].__len__() == 2  # nothing was restarted or replaced
    assert not [c for c in docker.calls if c[0] in ("rm", "restart", "stop", "kill")][0:0]


def test_service_enable_does_not_restart_either_container(up, docker, capsys):
    before = len(docker.calls)
    run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    assert not [c for c in docker.calls[before:] if c[0] in ("run", "rm", "restart", "stop", "kill", "start")]


def test_service_enable_leaves_the_other_services_closed(up, docker, capsys):
    run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    assert [c[3:5] for c in helper_calls(docker)] == [["enable", "dbx-web"]]


def test_service_enable_refuses_a_name_that_is_not_configured(up, docker, capsys):
    code, _, err = run(["service", "enable", "nope", str(up)], docker, capsys)
    assert code != 0 and "not configured" in err and "dbx-web" in err
    assert helper_calls(docker) == []


def test_service_enable_needs_a_running_session(configured, docker, capsys):
    docker.has_image()
    code, _, err = run(["service", "enable", "dbx-web", str(configured)], docker, capsys)
    assert code != 0 and "asb up" in err
    assert helper_calls(docker) == []


def test_service_enable_fails_when_netguard_stopped(up, docker, capsys):
    docker.kill(session_name(up.resolve()) + "-netguard")
    code, _, err = run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    assert code != 0 and "Netguard" in err and helper_calls(docker) == []


def test_service_enable_reports_a_failed_helper_as_an_error_and_not_as_enabled(up, docker, capsys):
    docker.set(service_fail="enable")
    code, out, err = run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    assert code != 0 and "enabled" not in out.lower() and "error" in err


def test_enabling_in_an_offline_session_is_refused_because_there_is_no_network(configured, docker):
    from agent_sandbox.runtime import Docker
    p = make_plan(configured, offline=True)
    with pytest.raises(host_services.ServiceError, match="offline"):
        host_services.enable(Docker(docker.command), p, "dbx-web")
    assert docker.calls == []


def test_service_disable_closes_one_service(up, docker, capsys):
    run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    code, out, _ = run(["service", "disable", "dbx-web", str(up)], docker, capsys)
    guard = session_name(up.resolve()) + "-netguard"
    assert code == 0 and "dbx-web" in out
    assert helper_calls(docker)[-1] == ["exec", guard, HELPER, "disable", "dbx-web"]


def test_service_disable_works_for_a_name_that_left_the_config(up, docker, capsys):
    (up / ".agent-sandbox.yaml").write_text("version: 1\n")
    code, _, _ = run(["service", "disable", "dbx-web", str(up)], docker, capsys)
    assert code == 0


def test_service_names_for_disable_are_validated_before_they_reach_docker(up, docker, capsys):
    code, _, err = run(["service", "disable", "a;b", str(up)], docker, capsys)
    assert code != 0 and helper_calls(docker) == []


def test_service_rejects_other_actions(up, docker, capsys):
    with pytest.raises(SystemExit):
        main(["service", "restart", "dbx-web", str(up)], docker=docker.command)


def test_down_removes_netguard_and_with_it_every_enabled_service(up, docker, capsys):
    run(["service", "enable", "dbx-web", str(up)], docker, capsys)
    run(["down", str(up)], docker, capsys)
    assert docker.containers == {}


# -- doctor -----------------------------------------------------------------------------------------


def test_doctor_reports_whether_each_host_service_answers_on_the_host_when_no_session_runs(
        configured, docker, capsys, monkeypatch):
    docker.has_image()
    monkeypatch.setattr(host_services, "probe_from_host", lambda e: e["name"] == "dbx-web")
    code, out, _ = run(["doctor", str(configured)], docker, capsys)
    assert code == 0  # a host app that is not started is not a broken setup
    assert "Host Service dbx-web host.docker.internal:4224" in out and "answers" in out
    line = [l for l in out.splitlines() if "llm-gateway" in l][0]
    assert "does not answer" in line and not line.startswith("ok")


def test_doctor_probes_from_the_session_when_it_runs_and_distinguishes_closed_from_down(
        up, docker, capsys, monkeypatch):
    monkeypatch.setattr(host_services, "enabled", lambda d, p: {"dbx-web"})
    monkeypatch.setattr(host_services, "probe_from_session", lambda d, p, e: e["name"] == "dbx-web")
    _, out, _ = run(["doctor", str(up)], docker, capsys)
    assert [l for l in out.splitlines() if "dbx-web" in l][0].startswith("ok")
    assert "not enabled" in [l for l in out.splitlines() if "llm-gateway" in l][0]
    assert "asb service enable llm-gateway" in out


def test_doctor_flags_an_enabled_service_that_the_session_cannot_reach(up, docker, capsys, monkeypatch):
    monkeypatch.setattr(host_services, "enabled", lambda d, p: {"dbx-web"})
    monkeypatch.setattr(host_services, "probe_from_session", lambda d, p, e: False)
    _, out, _ = run(["doctor", str(up)], docker, capsys)
    assert "does not answer" in [l for l in out.splitlines() if "dbx-web" in l][0]


def test_doctor_without_a_workspace_with_services_still_works(docker, capsys, monkeypatch, home):
    docker.has_image()
    monkeypatch.chdir(home)  # the home directory is not a usable Workspace
    code, out, _ = run(["doctor"], docker, capsys)
    assert code == 0 and "Docker daemon" in out and "Host Service" not in out
