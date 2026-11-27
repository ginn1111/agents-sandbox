"""Seam 1: `asb update` and `asb update --rollback` (ticket 11, ADR-0012). No Docker, no real upgrade."""
import sys
from pathlib import Path

import pytest

from agent_sandbox import update
from agent_sandbox.cli import main
from agent_sandbox.plan import session_name
from agent_sandbox.runtime import Result

from conftest import FakeDocker

UPDATE_FAKE = Path(__file__).with_name("fake_docker_update.py")
AGENT = "agent-sandbox-agent"
NETGUARD = "agent-sandbox-netguard"


class UpdateDocker(FakeDocker):
    def __init__(self, state_file):
        super().__init__(state_file)
        self.command = [sys.executable, str(UPDATE_FAKE)]

    def state(self):
        return self._state()

    def ids(self):
        return self._state().get("image_ids", {})

    def tags(self, repo):
        return sorted(t.split(":", 1)[1] for t in self._state()["images"] if t.rsplit(":", 1)[0] == repo)

    def id_of(self, ref):
        return self.ids().get(ref)

    def add_volumes(self, *names):
        s = self._state()
        s.setdefault("volumes", []).extend(names)
        self._write(s)

    def builds(self):
        return [c for c in self.calls if c[0] == "build"]

    def integration_runs(self):
        return [c for c in self.calls if c[0] == "run" and "--rm" in c]


class Runner:
    """The injected runner for host programs (uv, pipx, the upgraded asb). Records argument lists."""

    def __init__(self, docker=None, installed="uv", upgrade_code=0):
        self.calls = []
        self.docker_calls_at = []  # how many docker calls had happened when each runner call was made
        self.docker = docker
        self.installed = installed
        self.upgrade_code = upgrade_code

    def __call__(self, argv, passthrough=False):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)  # never a shell string
        self.calls.append(list(argv))
        self.docker_calls_at.append(len(self.docker.calls) if self.docker else 0)
        if argv[:3] == ["uv", "tool", "list"]:
            if self.installed == "uv":
                return Result(0, "agent-sandbox v0.1.0\n- agent-sandbox\n- asb\n", "")
            return Result(0, "ruff v0.5.0\n- ruff\n", "")
        if argv[:3] == ["pipx", "list", "--short"]:
            if self.installed == "pipx":
                return Result(0, "agent-sandbox 0.1.0\n", "")
            return Result(127, "", "pipx: not found")
        if argv[:3] == ["uv", "tool", "upgrade"] or argv[:2] == ["pipx", "upgrade"]:
            return Result(self.upgrade_code, "Updated agent-sandbox\n" if not self.upgrade_code else "",
                          "" if not self.upgrade_code else "network unreachable")
        return Result(0, "", "")


@pytest.fixture
def udocker(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(tmp_path / "docker-state.json"))
    d = UpdateDocker(tmp_path / "docker-state.json")
    d.has_image(f"{AGENT}:latest", f"{NETGUARD}:latest")  # as if `asb build` had run
    s = d._state()
    s["image_ids"] = {f"{AGENT}:latest": "sha256:0000000000a1", f"{NETGUARD}:latest": "sha256:0000000000a2"}
    d._write(s)
    return d


@pytest.fixture
def fresh(udocker):
    """No image at all yet: the first `asb update` is also the first build."""
    s = udocker._state()
    s["images"], s["image_ids"] = [], {}
    udocker._write(s)
    return udocker


@pytest.fixture
def day(monkeypatch):
    """Set the date `update` stamps builds with."""
    def set_day(iso):
        monkeypatch.setattr(update, "today", lambda: iso)
    set_day("2026-11-26")
    return set_day


@pytest.fixture
def runner(udocker):
    return Runner(udocker)


def run(argv, d, capsys, runner=None):
    code = main(argv, docker=d.command, runner=runner or Runner(d, installed=None))
    out = capsys.readouterr()
    return code, out.out, out.err


# -- order and --only

def test_update_runs_cli_then_image_then_integrations(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-hermes")
    r = Runner(udocker, installed="uv")
    code, out, _ = run(["update"], udocker, capsys, r)
    assert code == 0, out
    assert ["uv", "tool", "upgrade", "agent-sandbox"] in r.calls
    upgrade_at = r.docker_calls_at[r.calls.index(["uv", "tool", "upgrade", "agent-sandbox"])]
    first_build = udocker.calls.index(udocker.builds()[0])
    last_build = udocker.calls.index(udocker.builds()[-1])
    first_integration = udocker.calls.index(udocker.integration_runs()[0])
    assert upgrade_at <= first_build < last_build < first_integration
    assert [b[b.index("-t") + 1] for b in udocker.builds()] == [f"{AGENT}:2026-11-26", f"{NETGUARD}:2026-11-26"]


def test_only_cli_touches_neither_images_nor_volumes(udocker, day, capsys):
    r = Runner(udocker)
    code, _, _ = run(["update", "--only", "cli"], udocker, capsys, r)
    assert code == 0
    assert ["uv", "tool", "upgrade", "agent-sandbox"] in r.calls
    assert udocker.calls == []


def test_only_image_builds_both_images_and_runs_no_host_program(udocker, day, capsys):
    r = Runner(udocker)
    code, _, _ = run(["update", "--only", "image"], udocker, capsys, r)
    assert code == 0 and r.calls == []
    assert len(udocker.builds()) == 2 and udocker.integration_runs() == []


def test_only_integrations_builds_nothing(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-hermes")
    r = Runner(udocker)
    code, _, _ = run(["update", "--only", "integrations"], udocker, capsys, r)
    assert code == 0 and r.calls == []
    assert udocker.builds() == [] and len(udocker.integration_runs()) == 1


def test_only_and_rollback_cannot_be_combined(udocker, capsys):
    with pytest.raises(SystemExit):
        main(["update", "--only", "image", "--rollback"], docker=udocker.command)
    assert udocker.calls == []


def test_only_rejects_other_steps(udocker, capsys):
    with pytest.raises(SystemExit):
        main(["update", "--only", "volumes"], docker=udocker.command)


# -- cli step

def test_cli_upgrade_uses_uv_with_an_argument_list(udocker, capsys):
    r = Runner(udocker, installed="uv")
    run(["update", "--only", "cli"], udocker, capsys, r)
    assert r.calls == [["uv", "tool", "list"], ["uv", "tool", "upgrade", "agent-sandbox"]]


def test_cli_upgrade_uses_pipx_when_installed_with_pipx(udocker, capsys):
    r = Runner(udocker, installed="pipx")
    code, _, _ = run(["update", "--only", "cli"], udocker, capsys, r)
    assert code == 0
    assert ["pipx", "upgrade", "agent-sandbox"] in r.calls
    assert not any(c[:3] == ["uv", "tool", "upgrade"] for c in r.calls)


def test_cli_not_installed_by_uv_or_pipx_is_an_error_for_only_cli(udocker, capsys):
    r = Runner(udocker, installed=None)
    code, _, err = run(["update", "--only", "cli"], udocker, capsys, r)
    assert code == 1 and "uv tool" in err and "pipx" in err
    assert not any("upgrade" in c for c in r.calls)


def test_cli_not_installed_is_only_a_warning_in_a_full_update(udocker, day, capsys):
    code, out, err = run(["update"], udocker, capsys, Runner(udocker, installed=None))
    assert code == 0 and "warning" in err
    assert len(udocker.builds()) == 2  # the other steps still ran


def test_failed_cli_upgrade_is_reported_and_the_other_steps_still_run(udocker, day, capsys):
    code, _, err = run(["update"], udocker, capsys, Runner(udocker, upgrade_code=1))
    assert "network unreachable" in err and "warning" in err
    assert len(udocker.builds()) == 2


@pytest.mark.parametrize(("installer", "directory_query", "entrypoint"), [
    ("uv", ["uv", "tool", "dir"], "asb"),
    ("pipx", ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"], "agent-sandbox"),
])
def test_handoff_uses_the_upgraded_installation_even_with_a_shadowing_path(
    udocker, capsys, monkeypatch, tmp_path, installer, directory_query, entrypoint,
):
    tool_dir = tmp_path / "custom tool store"
    executable = tool_dir / "agent-sandbox" / "bin" / entrypoint
    shadow = tmp_path / "earlier-on-path" / "asb"
    for path in (executable, shadow):
        path.parent.mkdir(parents=True)
        path.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(shadow.parent))

    class Managed(Runner):
        def __call__(self, argv, passthrough=False):
            if argv == directory_query:
                return Result(0, f"{tool_dir}\n", "")
            return super().__call__(argv, passthrough)

    r = Managed(udocker, installed=installer)
    code, _, _ = run(["update"], udocker, capsys, r)
    assert code == 0
    assert r.calls[-1] == [str(executable), "update", "--after-cli"]
    assert udocker.calls == []  # the old process made no Docker calls


def test_handoff_lookup_failure_warns_and_continues_without_using_path(udocker, day, capsys, monkeypatch, tmp_path):
    shadow = tmp_path / "asb"
    shadow.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tmp_path))
    r = Runner(udocker)
    code, _, err = run(["update"], udocker, capsys, r)
    assert code == 0 and "could not locate" in err and "current CLI" in err
    assert not any("--after-cli" in cmd for cmd in r.calls)
    assert len(udocker.builds()) == 2


def test_after_cli_runs_image_and_integrations_without_upgrading_again(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-omp")
    r = Runner(udocker)
    code, _, _ = run(["update", "--after-cli"], udocker, capsys, r)
    assert code == 0 and r.calls == []
    assert len(udocker.builds()) == 2 and len(udocker.integration_runs()) == 1


def test_the_handoff_exit_code_is_the_update_exit_code(udocker, day, capsys, monkeypatch):
    monkeypatch.setattr(update, "find_cli", lambda tool, runner: "/x/asb")

    class Failing(Runner):
        def __call__(self, argv, passthrough=False):
            if argv[-1] == "--after-cli":
                self.calls.append(list(argv))
                return Result(3, "", "")
            return super().__call__(argv, passthrough)

    code, _, _ = run(["update"], udocker, capsys, Failing(udocker))
    assert code == 3


# -- builds: tags, latest, retention

def test_images_are_built_without_cache_at_latest_versions(udocker, day, capsys):
    run(["update", "--only", "image"], udocker, capsys)
    for b in udocker.builds():
        assert "--no-cache" in b and "--pull" in b
    ctxs = {b[-1] for b in udocker.builds()}
    assert len({Path(c).name for c in ctxs}) == 2 and {Path(c).name for c in ctxs} == {"agent", "netguard"}


def test_each_build_is_tagged_with_its_date_and_latest_points_at_it(udocker, day, capsys):
    code, out, _ = run(["update", "--only", "image"], udocker, capsys)
    assert code == 0
    for repo in (AGENT, NETGUARD):
        assert udocker.tags(repo) == ["2026-11-26", "latest", "previous"]
        assert udocker.id_of(f"{repo}:latest") == udocker.id_of(f"{repo}:2026-11-26")
    assert f"{AGENT}:2026-11-26" in out


def test_the_previous_build_is_kept_and_older_ones_are_pruned(udocker, day, capsys):
    for d in ("2026-11-24", "2026-11-25", "2026-11-26"):
        day(d)
        run(["update", "--only", "image"], udocker, capsys)
    for repo in (AGENT, NETGUARD):
        assert udocker.tags(repo) == ["2026-11-25", "2026-11-26", "latest", "previous"]
    assert udocker.id_of(f"{AGENT}:latest") == udocker.id_of(f"{AGENT}:2026-11-26")
    assert udocker.id_of(f"{AGENT}:previous") == udocker.id_of(f"{AGENT}:2026-11-25")


def test_a_second_update_on_the_same_day_never_overwrites_the_previous_build(udocker, day, capsys):
    run(["update", "--only", "image"], udocker, capsys)
    first = udocker.id_of(f"{AGENT}:2026-11-26")
    run(["update", "--only", "image"], udocker, capsys)
    assert udocker.id_of(f"{AGENT}:2026-11-26") == first
    assert udocker.tags(AGENT) == ["2026-11-26", "2026-11-26-2", "latest", "previous"]
    assert udocker.id_of(f"{AGENT}:latest") == udocker.id_of(f"{AGENT}:2026-11-26-2")


def test_a_build_from_plain_asb_build_becomes_the_previous_build(udocker, day, capsys):
    """`latest` made by `asb build` has no dated tag; update must not leave it with no name."""
    old = udocker.id_of(f"{AGENT}:latest")
    code, _, _ = run(["update", "--only", "image"], udocker, capsys)
    assert code == 0
    assert udocker.id_of(f"{AGENT}:previous") == old
    run(["update", "--rollback"], udocker, capsys)
    assert udocker.id_of(f"{AGENT}:latest") == old


def test_a_failed_build_leaves_latest_and_the_previous_build_alone(udocker, day, capsys):
    run(["update", "--only", "image"], udocker, capsys)
    before = {r: dict(udocker.ids()) for r in (AGENT,)}
    day("2026-11-27")
    udocker.set(build_fails=[NETGUARD])  # the agent image builds, then Netguard fails
    code, _, err = run(["update", "--only", "image"], udocker, capsys)
    assert code == 1 and "build failed" in err
    assert udocker.ids() == before[AGENT]
    assert udocker.tags(AGENT) == ["2026-11-26", "latest", "previous"] and udocker.tags(NETGUARD) == ["2026-11-26", "latest", "previous"]


def test_a_failed_agent_build_stops_before_integrations_and_changes_nothing(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-hermes")
    udocker.set(build_fails=True)
    code, _, _ = run(["update"], udocker, capsys, Runner(udocker, installed=None))
    assert code == 1 and udocker.integration_runs() == []
    assert udocker.tags(AGENT) == ["latest"]


def test_a_pruned_build_that_is_in_use_is_reported_not_fatal(udocker, day, capsys):
    for d in ("2026-11-24", "2026-11-25"):
        day(d)
        run(["update", "--only", "image"], udocker, capsys)
    udocker.set(rmi_fails=[f"{AGENT}:2026-11-24"])
    day("2026-11-26")
    code, _, err = run(["update", "--only", "image"], udocker, capsys)
    assert code == 0 and f"{AGENT}:2026-11-24" in err and "could not be removed" in err


def test_nothing_is_forced_when_pruning(udocker, day, capsys):
    for d in ("2026-11-24", "2026-11-25", "2026-11-26"):
        day(d)
        run(["update", "--only", "image"], udocker, capsys)
    rmis = [c for c in udocker.calls if c[0] == "rmi"]
    assert rmis and all("-f" not in c and "--force" not in c for c in rmis)


# -- rollback

def test_rollback_points_latest_back_at_the_previous_build(udocker, day, capsys):
    day("2026-11-25")
    run(["update", "--only", "image"], udocker, capsys)
    old = udocker.id_of(f"{AGENT}:latest")
    old_ng = udocker.id_of(f"{NETGUARD}:latest")
    day("2026-11-26")
    run(["update", "--only", "image"], udocker, capsys)
    assert udocker.id_of(f"{AGENT}:latest") != old
    code, out, _ = run(["update", "--rollback"], udocker, capsys)
    assert code == 0
    assert udocker.id_of(f"{AGENT}:latest") == old and udocker.id_of(f"{NETGUARD}:latest") == old_ng
    assert "2026-11-25" in out
    assert udocker.builds()[-1][-1].endswith("netguard")  # nothing was rebuilt by the rollback
    assert len(udocker.builds()) == 4


def test_rollback_with_no_previous_build_fails_and_changes_nothing(fresh, day, capsys):
    udocker = fresh
    run(["update", "--only", "image"], udocker, capsys)
    before = dict(udocker.ids())
    code, _, err = run(["update", "--rollback"], udocker, capsys)
    assert code == 1 and "no previous build" in err
    assert udocker.ids() == before


def test_rollback_only_goes_back_one_step(udocker, day, capsys):
    for d in ("2026-11-24", "2026-11-25", "2026-11-26"):
        day(d)
        run(["update", "--only", "image"], udocker, capsys)
    assert run(["update", "--rollback"], udocker, capsys)[0] == 0
    code, _, err = run(["update", "--rollback"], udocker, capsys)
    assert code == 1 and "no previous build" in err


def test_rollback_does_not_change_anything_if_one_image_has_no_previous_build(udocker, day, capsys):
    run(["update", "--only", "image"], udocker, capsys)  # both have only 11-26 (+ the adopted old latest)
    s = udocker.state()
    # Netguard loses its previous build, the agent image keeps it.
    s["images"].remove(f"{NETGUARD}:previous")
    s["image_ids"].pop(f"{NETGUARD}:previous")
    udocker._write(s)
    before = dict(udocker.ids())
    code, _, err = run(["update", "--rollback"], udocker, capsys)
    assert code == 1 and NETGUARD in err
    assert udocker.ids() == before


def test_an_update_after_a_rollback_keeps_the_build_rolled_back_to_as_previous(udocker, day, capsys):
    day("2026-11-25")
    run(["update", "--only", "image"], udocker, capsys)
    good = udocker.id_of(f"{AGENT}:latest")
    day("2026-11-26")
    run(["update", "--only", "image"], udocker, capsys)  # bad build
    run(["update", "--rollback"], udocker, capsys)
    day("2026-11-27")
    run(["update", "--only", "image"], udocker, capsys)
    assert udocker.tags(AGENT) == ["2026-11-25", "2026-11-27", "latest", "previous"]
    assert udocker.id_of(f"{AGENT}:2026-11-25") == good
    run(["update", "--rollback"], udocker, capsys)
    assert udocker.id_of(f"{AGENT}:latest") == good


def test_rollback_needs_no_runner_and_leaves_volumes_alone(udocker, day, capsys):
    day("2026-11-25")
    run(["update", "--only", "image"], udocker, capsys)
    day("2026-11-26")
    run(["update", "--only", "image"], udocker, capsys)
    r = Runner(udocker)
    before = len(udocker.calls)
    run(["update", "--rollback"], udocker, capsys, r)
    assert r.calls == []
    assert not [c for c in udocker.calls[before:] if c[0] in ("volume", "run", "rm", "stop", "kill")]


# -- running Sessions

def start_session(workspace, udocker, capsys):
    assert main(["up", str(workspace)], docker=udocker.command) == 0
    capsys.readouterr()
    return session_name(workspace.resolve())


def test_running_sessions_are_untouched_and_listed(workspace, udocker, day, capsys):
    name = start_session(workspace, udocker, capsys)
    before = set(udocker.containers)
    before_calls = len(udocker.calls)
    code, out, _ = run(["update", "--only", "image"], udocker, capsys)
    assert code == 0
    assert set(udocker.containers) == before and f"{name}-netguard" in udocker.containers
    assert not [c for c in udocker.calls[before_calls:] if c[0] in ("rm", "stop", "kill", "restart")]
    assert name in out and "restart to apply: asb down && asb up" in out


def test_no_sessions_means_no_restart_hint(udocker, day, capsys):
    _, out, _ = run(["update", "--only", "image"], udocker, capsys)
    assert "restart to apply" not in out


def test_rollback_lists_running_sessions_too(workspace, udocker, day, capsys):
    day("2026-11-25")
    run(["update", "--only", "image"], udocker, capsys)
    day("2026-11-26")
    run(["update", "--only", "image"], udocker, capsys)
    name = start_session(workspace, udocker, capsys)
    _, out, _ = run(["update", "--rollback"], udocker, capsys)
    assert name in out and "restart to apply: asb down && asb up" in out


def test_the_cli_step_alone_does_not_mention_sessions(workspace, udocker, capsys):
    start_session(workspace, udocker, capsys)
    _, out, _ = run(["update", "--only", "cli"], udocker, capsys, Runner(udocker))
    assert "restart to apply" not in out


# -- integrations in Agent volumes

def test_integrations_are_installed_in_each_existing_agent_volume(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-hermes", "agent-sandbox-omp", "agent-sandbox-claude", "agent-sandbox-mise")
    code, _, _ = run(["update", "--only", "integrations"], udocker, capsys)
    assert code == 0
    runs = udocker.integration_runs()
    assert sorted(r[-1] for r in runs) == ["hermes", "omp"]  # not claude, codex or mise
    hermes = next(r for r in runs if r[-1] == "hermes")
    assert "type=volume,source=agent-sandbox-hermes,target=/agents/hermes" in hermes
    assert "HOME=/agents/hermes" in hermes
    assert hermes[hermes.index("sh") - 1] == f"{AGENT}:latest"
    assert "/agents/hermes/.hermes" in hermes  # the directory the install needs, prepared first
    assert hermes[-3:] == ["sh", "/agents/hermes/.hermes", "hermes"]


def test_integration_runs_are_confined_like_a_session(udocker, capsys):
    udocker.add_volumes("agent-sandbox-hermes")
    run(["update", "--only", "integrations"], udocker, capsys)
    r = udocker.integration_runs()[0]
    assert "--read-only" in r and "--network" in r and r[r.index("--network") + 1] == "none"
    assert r[r.index("--cap-drop") + 1] == "ALL" and "no-new-privileges" in r
    assert "--privileged" not in r and "-v" not in r and "--volume" not in r
    assert not any(a.startswith("type=bind") for a in r)  # no host path
    assert r[r.index("--user") + 1].split(":")[0] != "0"


def test_agent_volumes_are_never_created_removed_or_recreated_by_update(udocker, day, capsys):
    udocker.add_volumes("agent-sandbox-hermes", "agent-sandbox-claude")
    run(["update"], udocker, capsys, Runner(udocker, installed=None))
    assert not [c for c in udocker.calls if c[0] == "volume" and c[1] in ("create", "rm", "prune")]
    assert not [c for c in udocker.calls if c[0] == "system"]
    assert udocker.state()["volumes"] == ["agent-sandbox-hermes", "agent-sandbox-claude"]
    for r in udocker.integration_runs():  # one command per volume: prepare the directory, install the integration
        assert r[-3] == "sh" and r[-2].startswith("/agents/") and r[-1] in ("hermes", "omp")


def test_no_agent_volumes_means_nothing_to_install(udocker, capsys):
    code, out, _ = run(["update", "--only", "integrations"], udocker, capsys)
    assert code == 0 and udocker.integration_runs() == [] and "No Agent volumes" in out


def test_a_failed_integration_install_is_reported_and_the_rest_continue(udocker, capsys):
    udocker.add_volumes("agent-sandbox-hermes", "agent-sandbox-omp")
    udocker.set(fail_integration=["hermes"])
    code, _, err = run(["update", "--only", "integrations"], udocker, capsys)
    assert code == 1 and "hermes" in err
    assert len(udocker.integration_runs()) == 2


def test_integrations_need_the_agent_image(udocker, capsys):
    udocker.add_volumes("agent-sandbox-hermes")
    s = udocker.state()
    s["images"].remove(f"{AGENT}:latest")
    udocker._write(s)
    code, _, err = run(["update", "--only", "integrations"], udocker, capsys)
    assert code == 1 and "asb build" in err and udocker.integration_runs() == []


def test_a_missing_docker_daemon_fails_the_image_step_cleanly(udocker, capsys):
    udocker.stop_daemon()
    code, _, err = run(["update", "--only", "image"], udocker, capsys)
    assert code == 1 and "Docker daemon" in err and udocker.builds() == []


def test_update_is_listed_in_help(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "update" in capsys.readouterr().out


def test_a_failed_promotion_puts_latest_back_and_removes_the_new_builds(udocker, day, capsys):
    old_agent = udocker.id_of(f"{AGENT}:latest")
    old_ng = udocker.id_of(f"{NETGUARD}:latest")
    udocker.set(tag_fails=[f"{NETGUARD}:latest"])  # the agent image is promoted first, then this fails
    code, _, err = run(["update", "--only", "image"], udocker, capsys)
    assert code == 1 and "error" in err
    assert udocker.id_of(f"{AGENT}:latest") == old_agent and udocker.id_of(f"{NETGUARD}:latest") == old_ng
    assert udocker.tags(AGENT) == ["latest"] and udocker.tags(NETGUARD) == ["latest"]


def test_the_untagged_build_adopted_as_previous_is_removed_by_the_next_update(udocker, day, capsys):
    adopted = udocker.id_of(f"{AGENT}:latest")
    for d in ("2026-11-25", "2026-11-26"):
        day(d)
        run(["update", "--only", "image"], udocker, capsys)
    assert adopted not in udocker.ids().values()
    assert udocker.state()["dangling"] == []


def test_default_runner_runs_an_argument_list_and_reports_a_missing_program():
    r = update.default_runner([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
    assert (r.code, r.out.strip(), r.err.strip()) == (3, "out", "err")
    assert update.default_runner(["/nonexistent/uv-binary", "tool"]).code == 127
    assert update.default_runner([sys.executable, "-c", "pass"], passthrough=True).code == 0


def test_rollback_and_after_cli_error_mentions_after_cli(capsys):
    with pytest.raises(SystemExit):
        main(["update", "--rollback", "--after-cli"])
    assert "--after-cli" in capsys.readouterr().err
