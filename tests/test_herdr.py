"""Seam 1: herdr as the Watcher inside the Session (ticket 07, ADR-0009). No Docker."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_sandbox.cli import IMAGE_CONTEXT, main
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan
from agent_sandbox.herdr import integration_steps

AGENT_BIN = IMAGE_CONTEXT / "asb-agent"


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def exec_calls(docker):
    return [c for c in docker.calls if c and c[0] == "exec" and "test" not in c]


def make_plan(ws):
    return plan(ws, load_config(ws))


# -- attach opens herdr

def test_attach_runs_herdr_in_the_session_at_the_workspace(workspace, docker, capsys):
    docker.has_image()
    code, _, _ = run(["attach", str(workspace)], docker, capsys)
    assert code == 0
    last = docker.calls[-1]
    name = make_plan(workspace).container_name
    assert last == ["exec", "-it", "-w", "/workspace", name, "herdr"]


def test_attach_starts_the_session_first_when_it_is_not_running(workspace, docker, capsys):
    docker.has_image()
    run(["attach", str(workspace)], docker, capsys)
    kinds = [c[0] for c in docker.calls]
    assert kinds.index("run") < len(kinds) - 1 and kinds[-1] == "exec"
    assert len(docker.containers) == 2


def test_attach_to_a_running_session_starts_nothing_new(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    before = sum(1 for c in docker.calls if c[0] == "run")
    run(["attach", str(workspace)], docker, capsys)
    assert sum(1 for c in docker.calls if c[0] == "run") == before
    assert docker.calls[-1][-1] == "herdr"


def test_attach_never_gives_the_inner_herdr_a_host_socket_or_host_environment(workspace, docker, capsys, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/run/user/1000/herdr.sock")
    monkeypatch.setenv("HERDR_SESSION", "host")
    docker.has_image()
    run(["attach", str(workspace)], docker, capsys)
    joined = json.dumps(docker.calls)
    assert "/run/user/1000/herdr.sock" not in joined and "HERDR_" not in joined


# -- nothing of the host's herdr reaches the Session

def test_the_session_mounts_no_herdr_socket_and_no_host_path_but_the_workspace(workspace, home):
    (home / ".config" / "herdr").mkdir(parents=True)
    (home / ".config" / "herdr" / "herdr.sock").write_text("")
    p = make_plan(workspace)


    binds = [a for a in p.session_run if a.startswith("type=bind")]
    assert len(binds) == 1 and str(workspace.resolve()) in binds[0]
    assert ".sock" not in " ".join(p.session_run)
    assert str(home / ".config") not in " ".join(p.session_run)
    assert not any(a.startswith("HERDR_") for a in p.session_run)
    assert "--privileged" not in p.session_run


# -- integrations are installed inside the Agent volumes

def test_integration_steps_cover_hermes_and_omp_with_their_own_home(workspace):
    steps = integration_steps(make_plan(workspace))
    by_agent = {s["agent"]: s for s in steps}
    assert set(by_agent) == {"hermes", "omp"}
    assert by_agent["hermes"]["prepare"] == ["mkdir", "-p", "/agents/hermes/.hermes"]
    assert by_agent["hermes"]["install"] == ["env", "HOME=/agents/hermes", "herdr", "integration", "install", "hermes"]
    assert by_agent["omp"]["prepare"] == ["mkdir", "-p", "/agents/omp/.omp/agent"]
    assert by_agent["omp"]["install"] == ["env", "HOME=/agents/omp", "herdr", "integration", "install", "omp"]


def test_agents_without_an_official_integration_get_none(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nagents:\n  aider: {command: [aider]}\n")
    assert {s["agent"] for s in integration_steps(make_plan(workspace))} == {"hermes", "omp"}


def test_up_installs_the_integrations_inside_the_session_after_it_starts(workspace, docker, capsys):
    docker.has_image()
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0
    name = make_plan(workspace).container_name
    calls = exec_calls(docker)
    assert ["exec", name, "mkdir", "-p", "/agents/hermes/.hermes"] in calls
    assert ["exec", name, "env", "HOME=/agents/hermes", "herdr", "integration", "install", "hermes"] in calls
    assert ["exec", name, "env", "HOME=/agents/omp", "herdr", "integration", "install", "omp"] in calls
    kinds = [c[0] for c in docker.calls]
    assert max(i for i, k in enumerate(kinds) if k == "run") < min(i for i, c in enumerate(docker.calls) if c[:1] == ["exec"] and "integration" in c)
    # every install runs after its own directory exists
    flat = [c for c in docker.calls if c[:1] == ["exec"]]
    for agent in ("hermes", "omp"):
        prep = next(i for i, c in enumerate(flat) if "mkdir" in c and f"/agents/{agent}" in " ".join(c))
        inst = next(i for i, c in enumerate(flat) if "integration" in c and c[-1] == agent)
        assert prep < inst


def test_attach_also_installs_them_on_a_fresh_session(workspace, docker, capsys):
    docker.has_image()
    run(["attach", str(workspace)], docker, capsys)
    assert any("integration" in c for c in exec_calls(docker))


def test_a_running_session_is_not_touched_again(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    n = len(exec_calls(docker))
    code, out, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0 and len(exec_calls(docker)) == n


def test_integrations_are_never_installed_when_the_session_does_not_start(workspace, docker, capsys):
    docker.has_image()
    docker.set(fail_run=[make_plan(workspace).container_name])
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 1
    assert not any("integration" in c for c in docker.calls)


def test_a_failed_integration_install_is_reported_but_keeps_the_session(workspace, docker, capsys):
    docker.has_image()
    docker.set(fail_exec=["omp"])
    code, out, err = run(["up", str(workspace)], docker, capsys)
    assert code == 0
    assert "omp" in err and "herdr" in err and "state" in err  # says what the user loses
    assert "hermes" not in err
    assert len(docker.containers) == 2


# -- herdr, the wrappers and the config are in the image

def dockerfile():
    return (IMAGE_CONTEXT / "Dockerfile").read_text()


def test_image_installs_herdr_and_checks_it_runs():
    d = dockerfile()
    assert "herdrdev/herdr/releases" in d and "/usr/local/bin/herdr" in d
    assert "herdr --version" in d
    assert "x86_64" in d and "aarch64" in d  # both Linux architectures (Colima on Apple silicon)


def test_image_points_herdr_at_a_read_only_config_that_skips_onboarding():
    d = dockerfile()
    assert "HERDR_CONFIG_PATH=/etc/asb/herdr.toml" in d
    cfg = (IMAGE_CONTEXT / "herdr.toml").read_text()
    assert "onboarding = false" in cfg


def test_image_puts_the_agent_wrappers_first_on_the_path():
    d = dockerfile()
    assert "/opt/asb/bin" in d and "asb-agent" in d
    for a in ("hermes", "omp", "claude", "codex"):
        assert a in d.split("asb-agent", 1)[1]


def test_session_runs_with_an_init_process_that_reaps_pane_processes(workspace):
    assert "--init" in make_plan(workspace).session_run


# -- the wrapper gives a pane's agent its own Agent volume as HOME

def _wrapper(tmp_path, name):
    """A wrapper symlink named like the agent, and a 'real' agent that just prints variables.

    /tmp may be mounted noexec, so the wrapper runs through `sh` and the real agent is printenv.
    """
    bindir = tmp_path / "asb-bin"
    bindir.mkdir()
    link = bindir / name
    link.symlink_to(AGENT_BIN)
    real = tmp_path / "real"
    real.mkdir()
    (real / name).symlink_to(shutil.which("printenv"))
    return link, bindir, real


@pytest.mark.parametrize("name", ["hermes", "omp", "claude", "codex"])
def test_wrapper_runs_the_real_agent_with_the_agents_home(tmp_path, name):
    link, bindir, real = _wrapper(tmp_path, name)
    env = {"PATH": f"{bindir}:{real}:/usr/bin:/bin", "HOME": "/tmp", "SECRET": "x"}
    r = subprocess.run(["sh", str(link), "HOME", "SECRET"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [f"/agents/{name}", "x"]  # HOME changed, the rest passed through


def test_wrapper_does_not_call_itself_when_the_real_agent_is_missing(tmp_path):
    link, bindir, real = _wrapper(tmp_path, "hermes")
    (real / "hermes").unlink()
    r = subprocess.run(["sh", str(link)], env={"PATH": f"{bindir}:/usr/bin:/bin"}, capture_output=True, text=True, timeout=10)
    assert r.returncode == 127 and "hermes" in r.stderr


def test_wrapper_script_is_executable_and_posix_sh():
    assert AGENT_BIN.stat().st_mode & 0o111 == 0o111
    assert AGENT_BIN.read_text().startswith("#!/bin/sh")
    assert subprocess.run(["sh", "-n", str(AGENT_BIN)]).returncode == 0
