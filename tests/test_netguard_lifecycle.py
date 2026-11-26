"""Seam 1: the Netguard lifecycle driven through `asb up/down/build/doctor` with the fake docker."""
import pytest

from agent_sandbox import runtime
from agent_sandbox.cli import main
from agent_sandbox.plan import session_name

NG_IMAGE = "agent-sandbox-netguard:latest"


@pytest.fixture(autouse=True)
def fast_ready(monkeypatch):
    monkeypatch.setattr(runtime, "READY_TIMEOUT", 0.3)
    monkeypatch.setattr(runtime, "READY_POLL", 0.01)


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def runs(docker):
    """Names of the containers `docker run` created, in order."""
    out = []
    for c in docker.calls:
        if c[0] == "run":
            out.append(c[c.index("--name") + 1])
    return out


def names(workspace):
    s = session_name(workspace.resolve())
    return s, f"{s}-netguard"


def test_up_rejects_a_non_linux_docker_daemon_before_starting_netguard(workspace, docker, capsys):
    docker.has_image()
    docker.set(server_os="windows")
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "Linux Docker daemon" in err and runs(docker) == []


def test_up_starts_netguard_first_and_the_session_only_after_it_is_ready(workspace, docker, capsys):
    docker.has_image()
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    session, guard = names(workspace)
    assert code == 0 and runs(docker) == [guard, session]
    calls = docker.calls
    run_guard = next(i for i, c in enumerate(calls) if c[0] == "run" and guard in c)
    run_session = next(i for i, c in enumerate(calls) if c[0] == "run" and session in c)
    probes = [i for i, c in enumerate(calls) if c[0] == "exec" and guard in c and "test" in c]
    assert probes and run_guard < min(probes) and max(probes) < run_session


def test_the_ready_probe_checks_the_ready_file_in_the_netguard_container(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    _, guard = names(workspace)
    probe = [c for c in docker.calls if c[0] == "exec"][0]
    assert probe == ["exec", guard, "test", "-f", "/run/netguard/ready"]


def test_the_session_never_starts_when_netguard_is_not_ready(workspace, docker, capsys):
    docker.has_image()
    docker.set(netguard_ready=False)
    code, _, err = run(["up", str(workspace)], docker, capsys)
    session, guard = names(workspace)
    assert code != 0 and "not ready" in err and "Netguard" in err
    assert session not in runs(docker)
    assert docker.containers == {}  # the half-started Netguard is removed


def test_the_session_never_starts_when_netguard_exits(workspace, docker, capsys):
    docker.has_image()
    docker.set(netguard_exits=True)
    code, _, err = run(["up", str(workspace)], docker, capsys)
    session, _ = names(workspace)
    assert code != 0 and "Netguard" in err
    assert session not in runs(docker) and docker.containers == {}
    assert any(c[0] == "logs" for c in docker.calls)  # the reason is surfaced


def test_up_fails_and_cleans_up_when_netguard_cannot_be_created(workspace, docker, capsys):
    docker.has_image()
    docker.set(fail_run=[names(workspace)[1]])
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "run refused" in err
    assert docker.containers == {}


def test_a_failed_session_start_removes_netguard_too(workspace, docker, capsys):
    docker.has_image()
    docker.set(fail_run=[names(workspace)[0]])
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and docker.containers == {}


def test_netguard_image_missing_fails_before_anything_starts(workspace, docker, capsys):
    docker.has_image("agent-sandbox-agent:latest")
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and NG_IMAGE in err and "asb build" in err
    assert runs(docker) == []


def test_offline_starts_only_the_session_on_the_none_network(workspace, docker, capsys):
    docker.has_image("agent-sandbox-agent:latest")  # the Netguard image is not needed offline
    code, _, _ = run(["up", "--offline", str(workspace)], docker, capsys)
    session, _ = names(workspace)
    assert code == 0 and runs(docker) == [session]
    (c,) = docker.containers.values()
    assert c["args"][c["args"].index("--network") + 1] == "none"
    # no Netguard readiness probe; the only exec calls are the herdr integration installs in the Session
    assert not any(c[0] == "exec" and c[1] != session for c in docker.calls)
    assert not any(c[0] == "exec" and "test" in c for c in docker.calls)


def test_up_twice_does_not_start_a_second_netguard(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    before = runs(docker)
    code, out, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0 and "already running" in out and runs(docker) == before


def test_up_refuses_a_running_session_whose_netguard_has_stopped(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    _, guard = names(workspace)
    docker.kill(guard)
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "Netguard" in err and "asb down" in err


def test_a_stale_netguard_from_a_crashed_run_is_replaced(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    session, guard = names(workspace)
    runtime.Docker(docker.command).run("rm", "-f", session)  # only the Session is gone
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0 and set(docker.containers) == {session, guard}


def test_down_removes_the_session_then_netguard(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    session, guard = names(workspace)
    code, _, _ = run(["down", str(workspace)], docker, capsys)
    removals = [c[-1] for c in docker.calls if c[0] == "rm" and c[-1] in (session, guard)]
    assert code == 0 and docker.containers == {}
    assert removals[-2:] == [session, guard]  # the Session never outlives its Netguard's rules


def test_down_removes_an_orphaned_netguard(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    session, _ = names(workspace)
    runtime.Docker(docker.command).run("rm", "-f", session)
    code, _, _ = run(["down", str(workspace)], docker, capsys)
    assert code == 0 and docker.containers == {}


def test_concurrent_sessions_each_have_their_own_netguard(home, docker, capsys):
    docker.has_image()
    a, b = home / "one" / "api", home / "two" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    run(["up", str(a)], docker, capsys)
    run(["up", str(b)], docker, capsys)
    assert len(docker.containers) == 4
    sa, ga = names(a)
    sb, gb = names(b)
    nets = {n: docker.containers[n]["args"][docker.containers[n]["args"].index("--network") + 1] for n in (sa, sb)}
    assert nets == {sa: f"container:{ga}", sb: f"container:{gb}"}
    run(["down", str(a)], docker, capsys)
    assert set(docker.containers) == {sb, gb}  # the other Session keeps its own Netguard


def test_ls_lists_sessions_not_netguards(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    _, out, _ = run(["ls"], docker, capsys)
    assert len(out.strip().splitlines()) == 1 and "netguard" not in out


def test_build_builds_the_netguard_image_from_its_own_context(docker, capsys):
    code, _, _ = run(["build"], docker, capsys)
    builds = [c for c in docker.calls if c[0] == "build"]
    assert code == 0 and [c[c.index("-t") + 1] for c in builds][:2] == ["agent-sandbox-agent:latest", NG_IMAGE]
    assert builds[1][-1].endswith("netguard")


def test_doctor_requires_the_netguard_image(docker, capsys):
    docker.has_image("agent-sandbox-agent:latest")
    code, out, _ = run(["doctor"], docker, capsys)
    assert code != 0 and "netguard" in out.lower() and "asb build" in out
    docker.has_image(NG_IMAGE)
    code, _, _ = run(["doctor"], docker, capsys)
    assert code == 0


def test_no_docker_command_uses_a_shell_string(workspace, docker, capsys):
    docker.has_image()
    run(["up", str(workspace)], docker, capsys)
    assert all(isinstance(a, str) for c in docker.calls for a in c)
    assert not any(a in ("sh", "-c") for c in docker.calls if c[0] == "run" for a in c)
