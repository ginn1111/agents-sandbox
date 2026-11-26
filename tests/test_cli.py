import json
from importlib.metadata import entry_points

import pytest

from agent_sandbox.cli import main


def run(argv, docker, capsys, cwd=None):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_both_entry_points_run_the_same_function():
    eps = {e.name: e.value for e in entry_points(group="console_scripts") if e.name in ("asb", "agent-sandbox")}
    assert eps == {"asb": "agent_sandbox.cli:main", "agent-sandbox": "agent_sandbox.cli:main"}


def test_check_prints_the_plan_and_never_calls_docker(workspace, docker, capsys):
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0
    assert f"Workspace: {workspace.resolve()}" in out
    assert "Config:" in out and "defaults" in out
    assert "Limits:" in out and "filename" in out
    assert "Result: Ready" in out
    assert docker.calls == []


def test_check_defaults_to_the_current_directory(workspace, docker, capsys, monkeypatch):
    monkeypatch.chdir(workspace)
    code, out, _ = run(["check"], docker, capsys)
    assert code == 0 and str(workspace.resolve()) in out


def test_check_json_is_the_same_plan(workspace, docker, capsys):
    code, out, _ = run(["check", "--json", str(workspace)], docker, capsys)
    data = json.loads(out)
    assert code == 0
    assert data["workspace"] == str(workspace.resolve())
    assert data["result"] == "Ready"
    assert data["session_run"][0] == "run"


def test_check_never_claims_protection_it_does_not_give(workspace, docker, capsys):
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    lowered = out.lower()
    assert "fully secure" not in lowered and "safe from" not in lowered and "protected against" not in lowered


def test_check_lists_project_config_rules(workspace, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nsecrets: {allow: ['**/.env.local']}\n")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert ".agent-sandbox.yaml" in out
    assert "Project config: secrets.allow **/.env.local" in out


def test_check_refuses_home_with_nonzero_exit_and_clear_error(home, docker, capsys):
    code, out, err = run(["check", str(home)], docker, capsys)
    assert code != 0
    assert "home directory" in err
    assert out == ""


@pytest.mark.parametrize("target", ["/", None])
def test_check_refuses_root_and_above_home(home, docker, capsys, target):
    path = target or str(home.parent)
    code, _, err = run(["check", path], docker, capsys)
    assert code != 0 and "refusing" in err


def test_invalid_config_exits_nonzero_with_the_key_named(workspace, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nresorces: {}\n")
    code, _, err = run(["check", str(workspace)], docker, capsys)
    assert code != 0 and "resorces" in err


def test_up_starts_one_session_with_the_planned_arguments(workspace, docker, capsys):
    docker.has_image()
    code, out, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0
    assert len(docker.containers) == 1
    (c,) = docker.containers.values()
    assert c["args"][:2] == ["run", "-d"]
    assert "--read-only" in c["args"]
    assert "started" in out.lower()


def test_up_twice_is_a_noop_that_reports_the_running_session(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    runs_before = [c for c in docker.calls if c[0] == "run"]
    code, out, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0
    assert "already running" in out
    assert [c for c in docker.calls if c[0] == "run"] == runs_before


def test_up_fails_clearly_when_the_image_is_missing(workspace, docker, capsys):
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "asb build" in err
    assert docker.containers == {}


def test_up_fails_clearly_when_the_daemon_is_down(workspace, docker, capsys):
    docker.has_image()
    docker.stop_daemon()
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "daemon" in err.lower()


def test_up_refuses_unsafe_workspace_before_touching_docker(home, docker, capsys):
    code, _, _ = run(["up", str(home)], docker, capsys)
    assert code != 0 and docker.calls == []


def test_two_workspaces_run_side_by_side(home, docker, capsys):
    docker.has_image()
    a, b = home / "one" / "api", home / "two" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    run(["up", str(a)], docker, capsys)
    run(["up", str(b)], docker, capsys)
    assert len(docker.containers) == 2


def test_ls_lists_running_sessions_with_their_workspace(home, docker, capsys):
    docker.has_image()
    a = home / "one" / "api"
    a.mkdir(parents=True)
    run(["up", str(a)], docker, capsys)
    code, out, _ = run(["ls"], docker, capsys)
    assert code == 0 and str(a.resolve()) in out


def test_ls_with_nothing_running(docker, capsys):
    code, out, _ = run(["ls"], docker, capsys)
    assert code == 0 and "No running Sessions" in out


def test_down_removes_the_session(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    code, _, _ = run(["down", str(workspace)], docker, capsys)
    assert code == 0 and docker.containers == {}


def test_down_never_removes_volumes(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    run(["down", str(workspace)], docker, capsys)
    for call in docker.calls:  # volumes may be created, never removed
        assert call[:2] not in (["volume", "rm"], ["volume", "prune"]) and "-v" not in call


def test_down_when_nothing_runs_is_fine(workspace, docker, capsys):
    code, out, _ = run(["down", str(workspace)], docker, capsys)
    assert code == 0 and "not running" in out


def test_attach_starts_the_session_then_opens_a_shell(workspace, docker, capsys):
    docker.has_image()
    code, _, _ = run(["attach", str(workspace)], docker, capsys)
    assert code == 0
    assert len(docker.containers) == 1
    last = docker.calls[-1]
    assert last[0] == "exec" and "-it" in last and "bash" in last


def test_doctor_reports_docker_python_and_image(docker, capsys):
    code, out, _ = run(["doctor"], docker, capsys)
    assert "Docker daemon" in out and "Python" in out and "agent image" in out.lower()
    assert code != 0  # image missing
    docker.has_image()
    code, out, _ = run(["doctor"], docker, capsys)
    assert code == 0


def test_doctor_fails_when_docker_is_missing(capsys):
    code = main(["doctor"], docker=["/nonexistent/docker-binary"])
    out = capsys.readouterr().out
    assert code != 0 and "docker" in out.lower() and "not found" in out.lower()


def test_build_builds_the_agent_image(docker, capsys):
    code, _, _ = run(["build"], docker, capsys)
    assert code == 0
    build = [c for c in docker.calls if c[0] == "build"][0]
    assert "agent-sandbox-agent:latest" in build
