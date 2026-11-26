"""Seam 1: the Host Browser (ADR-0008, ADR-0013, ADR-0021). No Docker, no Chrome.

Covers who may make it available, what `check`/`up`/`browser enable` say, what `browser enable|disable`
runs (one Netguard helper call, nothing else), what `doctor` prints, and that the CLI never runs
Chrome or any other host program.
"""
import json
import shlex
import subprocess
import sys

import pytest

from agent_sandbox import host_browser
from agent_sandbox.cli import main
from agent_sandbox.config import ConfigError, load_config
from agent_sandbox.plan import plan, session_name

HELPER = "/usr/local/bin/asb-service"
GLOBAL_ON = "version: 1\nnetwork:\n  host_browser: true\n"
WARNING = "agent controls host Chrome"


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def set_global(home, text):
    path = home / ".config" / "agent-sandbox" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def enabled_in_global(home):
    return set_global(home, GLOBAL_ON)


def helper_calls(docker):
    return [c for c in docker.calls if c[:1] == ["exec"] and HELPER in c]


def guard_of(ws):
    return session_name(ws.resolve()) + "-netguard"


@pytest.fixture
def up(workspace, enabled_in_global, docker, capsys):
    docker.has_image()
    assert run(["up", str(workspace)], docker, capsys)[0] == 0
    return workspace


# -- who may make it available ----------------------------------------------------------------------


def test_it_is_off_by_default(workspace):
    p = plan(workspace, load_config(workspace))
    assert p.host_browser is None
    assert not any("Host Browser" in w for w in p.warnings)


def test_project_config_cannot_enable_it_and_the_message_says_where_to_do_it(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nnetwork: {host_browser: true}\n")
    with pytest.raises(ConfigError) as e:
        load_config(workspace)
    msg = str(e.value)
    assert "host_browser" in msg and "project config" in msg and "global config" in msg and "CLI" in msg


def test_check_and_up_refuse_a_project_config_that_enables_it(workspace, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nnetwork: {host_browser: true}\n")
    docker.has_image()
    for cmd in ("check", "up"):
        code, _, err = run([cmd, str(workspace)], docker, capsys)
        assert code == 1 and "host_browser" in err and "global config" in err
    assert docker.calls == []


def test_global_config_makes_it_available(workspace, enabled_in_global):
    p = plan(workspace, load_config(workspace))
    assert p.host_browser["available"] is True and p.host_browser["active"] is False


def test_the_cli_flag_makes_it_available_for_that_command_only(workspace, docker, capsys):
    code, out, _ = run(["check", "--host-browser", str(workspace)], docker, capsys)
    assert code == 0 and WARNING in out
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert WARNING not in out


def test_a_project_config_without_the_key_does_not_change_the_global_choice(workspace, enabled_in_global):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nresources: {cpus: 2}\n")
    assert plan(workspace, load_config(workspace)).host_browser["available"] is True


def test_a_host_service_may_not_take_the_name_or_address_the_host_browser_uses(workspace):
    for entry in ("{name: host-browser, host: host.docker.internal, port: 4224}",
                  "{name: chrome, host: host.docker.internal, port: 9222}"):
        (workspace / ".agent-sandbox.yaml").write_text(f"version: 1\nnetwork:\n  host_services:\n    - {entry}\n")
        with pytest.raises(ConfigError, match="Host Browser"):
            load_config(workspace)


# -- the warning ------------------------------------------------------------------------------------


def test_check_warns_when_it_is_available_and_says_it_is_not_enabled_yet(workspace, enabled_in_global, docker, capsys):
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0 and docker.calls == []
    assert WARNING in out and "intranet" in out and "file:///" in out
    assert "asb browser enable" in out


def test_check_lists_it_as_off_when_it_is_not_available(workspace, docker, capsys):
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert "Host Browser:   off" in out and WARNING not in out


def test_check_json_carries_the_host_browser_entry_and_its_warning(workspace, enabled_in_global, docker, capsys):
    _, out, _ = run(["check", "--json", str(workspace)], docker, capsys)
    data = json.loads(out)
    assert data["host_browser"]["host"] == "host.docker.internal" and data["host_browser"]["port"] == 9222
    assert any(WARNING in w for w in data["warnings"])


def test_up_prints_the_warning_on_every_session_where_it_is_available(workspace, enabled_in_global, docker, capsys):
    docker.has_image()
    _, out, err = run(["up", str(workspace)], docker, capsys)
    assert WARNING in out + err and "asb browser enable" in out + err
    _, out, err = run(["up", str(workspace)], docker, capsys)  # an already running Session
    assert "already running" in out and WARNING in out + err


def test_up_is_silent_about_it_when_it_is_off(workspace, docker, capsys):
    docker.has_image()
    _, out, err = run(["up", str(workspace)], docker, capsys)
    assert "Host Browser" not in out + err and WARNING not in out + err


def test_offline_plans_have_no_host_browser_and_say_why(workspace, enabled_in_global):
    p = plan(workspace, load_config(workspace, cli={"offline": True}))
    assert p.host_browser is None
    assert any("offline" in w and "Host Browser" in w for w in p.warnings)


def test_a_profile_directory_inside_the_workspace_blocks_the_plan(workspace, enabled_in_global, monkeypatch, docker, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(workspace / ".state"))  # the agent could read the cookies there
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 1 and "Error:" in out and "Workspace" in out and "Result: Blocked" in out


# -- up never opens a path to Chrome ----------------------------------------------------------------


def test_netguard_starts_with_no_rule_for_chrome(workspace, enabled_in_global):
    p = plan(workspace, load_config(workspace))
    assert not [l for l in p.netguard_rules if "9222" in l or "--dport" in l]
    assert not any("9222" in a for a in p.netguard_run) and not any("9222" in a for a in p.session_run)


def test_up_does_not_open_a_path_to_chrome(up, docker):
    assert helper_calls(docker) == []


# -- browser enable / disable -----------------------------------------------------------------------


def test_enable_runs_the_netguard_helper_for_port_9222_on_the_host_gateway_name_and_nothing_else(
        up, docker, capsys):
    before = len(docker.calls)
    code, out, err = run(["browser", "enable", str(up)], docker, capsys)
    assert code == 0, err
    assert helper_calls(docker) == [
        ["exec", guard_of(up), HELPER, "enable", "host-browser", "host.docker.internal", "9222"]]
    new = docker.calls[before:]
    assert not [c for c in new if c[0] in ("run", "rm", "stop", "restart", "kill", "build", "network")]
    assert WARNING in out + err  # said again at the moment it matters
    assert host_browser.MCP_COMMAND in out  # how the agent connects


def test_enable_does_not_start_chrome_and_says_the_human_must(up, docker, capsys):
    _, out, _ = run(["browser", "enable", str(up)], docker, capsys)
    assert "asb doctor" in out and "never starts Chrome" in out


def test_enable_works_with_the_cli_flag_alone(workspace, docker, capsys):
    docker.has_image()
    assert run(["up", str(workspace)], docker, capsys)[0] == 0
    code, _, err = run(["browser", "enable", "--host-browser", str(workspace)], docker, capsys)
    assert code == 0, err
    assert len(helper_calls(docker)) == 1


def test_enable_is_refused_when_neither_global_config_nor_the_flag_made_it_available(workspace, docker, capsys):
    docker.has_image()
    assert run(["up", str(workspace)], docker, capsys)[0] == 0
    code, _, err = run(["browser", "enable", str(workspace)], docker, capsys)
    assert code == 1 and "not available" in err and "global config" in err and "--host-browser" in err
    assert helper_calls(docker) == []


def test_enable_needs_a_running_session(workspace, enabled_in_global, docker, capsys):
    docker.has_image()
    code, _, err = run(["browser", "enable", str(workspace)], docker, capsys)
    assert code == 1 and "asb up" in err and helper_calls(docker) == []


def test_enable_fails_closed_when_netguard_has_stopped(up, docker, capsys):
    docker.kill(guard_of(up))
    code, _, err = run(["browser", "enable", str(up)], docker, capsys)
    assert code == 1 and "Netguard" in err and helper_calls(docker) == []


def test_a_failed_helper_is_an_error_and_nothing_is_reported_as_enabled(up, docker, capsys):
    docker.set(service_fail="enable")
    code, out, err = run(["browser", "enable", str(up)], docker, capsys)
    assert code == 1 and "asb-service" in err and "enabled" not in out.lower()
    assert host_browser.SERVICE not in docker._state().get("services", {}).get(guard_of(up), {})


def test_enable_is_refused_when_the_session_is_offline(workspace, enabled_in_global, docker, capsys):
    docker.has_image()
    assert run(["up", "--offline", str(workspace)], docker, capsys)[0] == 0
    code, _, err = run(["browser", "enable", "--offline", str(workspace)], docker, capsys)
    assert code == 1 and "offline" in err and helper_calls(docker) == []


def test_enable_is_refused_when_the_plan_is_blocked(up, monkeypatch, docker, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(up / ".state"))
    code, _, err = run(["browser", "enable", str(up)], docker, capsys)
    assert code == 1 and "Workspace" in err and helper_calls(docker) == []


def test_disable_closes_it_even_after_the_config_changed(up, enabled_in_global, docker, capsys):
    assert run(["browser", "enable", str(up)], docker, capsys)[0] == 0
    assert host_browser.SERVICE in docker._state()["services"][guard_of(up)]
    (up / ".agent-sandbox.yaml").write_text("version: 1\nnetwork: {host_browser: true}\n")  # now invalid
    code, out, _ = run(["browser", "disable", str(up)], docker, capsys)
    assert code == 0 and "disabled" in out
    assert host_browser.SERVICE not in docker._state()["services"][guard_of(up)]
    assert helper_calls(docker)[-1] == ["exec", guard_of(up), HELPER, "disable", "host-browser"]


def test_disable_needs_a_running_session(workspace, docker, capsys):
    docker.has_image()
    code, _, err = run(["browser", "disable", str(workspace)], docker, capsys)
    assert code == 1 and "asb up" in err


def test_the_path_lives_in_netguard_so_down_removes_it_with_the_session(up, docker, capsys):
    run(["browser", "enable", str(up)], docker, capsys)
    run(["down", str(up)], docker, capsys)
    assert guard_of(up) not in docker.containers


def test_enabling_the_browser_opens_no_host_service(workspace, enabled_in_global, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nnetwork:\n  host_services:\n    - {name: dbx-web, host: host.docker.internal, port: 4224}\n")
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    run(["browser", "enable", str(workspace)], docker, capsys)
    assert list(docker._state()["services"][guard_of(workspace)]) == ["host-browser"]


# -- the CLI never runs Chrome or any host program --------------------------------------------------


def test_no_command_runs_anything_on_the_host_but_the_docker_cli(up, docker, capsys, monkeypatch):
    started = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, argv, *a, **kw):
            started.append(list(argv))
            super().__init__(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    for argv in (["check", str(up)], ["up", str(up)], ["browser", "enable", str(up)],
                 ["doctor", str(up)], ["browser", "disable", str(up)]):
        run(argv, docker, capsys)
    assert started, "the spy saw nothing"
    assert all(a[:2] == docker.command for a in started), started


def test_no_command_ever_opens_a_browser(up, docker, capsys, monkeypatch):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("opened a browser"))
    for argv in (["check", str(up)], ["browser", "enable", str(up)], ["doctor", str(up)]):
        run(argv, docker, capsys)


# -- doctor -----------------------------------------------------------------------------------------


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state dir"  # a space, on purpose
    monkeypatch.setenv("XDG_STATE_HOME", str(d))
    return d


def test_doctor_prints_the_exact_launch_command_with_a_dedicated_profile(workspace, docker, capsys, state_dir):
    docker.has_image()
    code, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert code == 0  # Chrome not running is not a broken setup
    assert host_browser.launch_command() in out
    profile = host_browser.profile_dir()
    assert profile == state_dir / "agent-sandbox" / "chrome-profile"
    assert shlex.quote(f"--user-data-dir={profile}") in out  # quoted: the path has a space
    assert "--remote-debugging-port=9222" in out


def test_the_launch_command_never_uses_an_everyday_chrome_profile(home):
    for platform in ("linux", "darwin"):
        cmd = host_browser.launch_command(platform=platform)
        assert "--user-data-dir=" in cmd
        for everyday in (".config/google-chrome", ".config/chromium", "Application Support/Google/Chrome"):
            assert everyday not in cmd
        assert str(home) in cmd  # under the user's state directory, away from any Workspace


def test_the_launch_command_names_the_right_binary_per_platform(home):
    assert host_browser.launch_command(platform="linux").startswith("google-chrome ")
    assert host_browser.launch_command(platform="darwin").startswith(
        "'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' ")


def test_doctor_prints_the_launch_command_without_a_workspace_and_without_docker(home, monkeypatch, capsys):
    monkeypatch.chdir(home)  # the home directory is not a usable Workspace
    code = main(["doctor"], docker=["/nonexistent/docker-binary"])
    out = capsys.readouterr().out
    assert code != 0 and host_browser.launch_command() in out


def test_doctor_says_asb_never_starts_chrome_and_what_enabling_exposes(workspace, docker, capsys):
    docker.has_image()
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert "never starts Chrome" in out and "intranet" in out and "file:///" in out


def test_doctor_reports_whether_chrome_answers_on_the_host(workspace, docker, capsys, monkeypatch):
    docker.has_image()
    monkeypatch.setattr(host_browser, "probe_host", lambda: False)
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert [l for l in out.splitlines() if "no Chrome answers" in l][0].startswith("info")
    monkeypatch.setattr(host_browser, "probe_host", lambda: True)
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert [l for l in out.splitlines() if "Chrome answers on" in l][0].startswith("ok")


def test_doctor_points_at_the_enable_command_when_it_is_available_but_not_enabled(up, docker, capsys):
    _, out, _ = run(["doctor", str(up)], docker, capsys)
    assert "asb browser enable" in out


def test_doctor_says_how_to_make_it_available_when_it_is_not(workspace, docker, capsys):
    docker.has_image()
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert "network.host_browser" in out and "global config" in out and "--host-browser" in out


def test_doctor_probes_from_the_session_once_it_is_enabled(up, docker, capsys, monkeypatch):
    run(["browser", "enable", str(up)], docker, capsys)
    monkeypatch.setattr(host_browser, "probe_session", lambda d, p: (True, "Chrome/130"))
    _, out, _ = run(["doctor", str(up)], docker, capsys)
    assert [l for l in out.splitlines() if "from the Session" in l][0].startswith("ok")
    monkeypatch.setattr(host_browser, "probe_session", lambda d, p: (False, "connection refused"))
    _, out, _ = run(["doctor", str(up)], docker, capsys)
    line = [l for l in out.splitlines() if "from the Session" in l][0]
    assert line.startswith("warn") and "127.0.0.1" in line and "connection refused" in line


def test_the_relay_command_listens_on_the_docker_gateway_only(home):
    cmd = host_browser.relay_command("172.17.0.1")
    assert cmd == "socat TCP-LISTEN:9222,bind=172.17.0.1,fork,reuseaddr TCP:127.0.0.1:9222"


def test_the_relay_command_refuses_anything_that_is_not_a_plain_usable_ipv4_address(home):
    for bad in ("0.0.0.0", "127.0.0.1", "", "172.17.0.1; rm -rf /", "$(id)", "::1", "169.254.169.254"):
        assert host_browser.relay_command(bad) is None


def test_on_linux_doctor_shows_the_relay_with_the_gateway_docker_reports(workspace, docker, capsys, monkeypatch):
    docker.has_image()
    monkeypatch.setattr(host_browser, "bridge_gateway", lambda d: "172.17.0.1")
    monkeypatch.setattr(sys, "platform", "linux")
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert "socat TCP-LISTEN:9222,bind=172.17.0.1" in out and "loopback" in out


def test_on_macos_doctor_shows_no_relay(workspace, docker, capsys, monkeypatch):
    docker.has_image()
    monkeypatch.setattr(sys, "platform", "darwin")
    _, out, _ = run(["doctor", str(workspace)], docker, capsys)
    assert "socat" not in out
