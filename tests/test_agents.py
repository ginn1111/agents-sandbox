"""Seam 1: Agent volumes, per-agent env and the mise volume, as seen in the SessionPlan (ticket 06)."""
import json
from pathlib import Path

import pytest

from agent_sandbox.cli import IMAGE_CONTEXT, main
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan

DEFAULT_AGENTS = ["claude", "codex", "hermes", "omp"]


def make_plan(ws, **cli):
    return plan(ws, load_config(ws, cli=cli or None))


def mount_specs(args):
    """Every --mount argument parsed into a dict (CSV-quoted fields)."""
    import csv

    out = []
    for i, a in enumerate(args):
        if a == "--mount":
            (row,) = csv.reader([args[i + 1]])
            out.append(dict(f.split("=", 1) if "=" in f else (f, "") for f in row))
    return out


def volume_mounts(args):
    return [m for m in mount_specs(args) if m["type"] == "volume"]


def test_each_default_agent_gets_its_own_volume_mounted_as_its_home(workspace):
    p = make_plan(workspace)
    by_source = {m["source"]: m["target"] for m in volume_mounts(p.session_run)}
    for name in DEFAULT_AGENTS:
        assert by_source[f"agent-sandbox-{name}"] == f"/agents/{name}"


def test_agent_volumes_are_never_shared_between_agents(workspace):
    p = make_plan(workspace)
    mounts = volume_mounts(p.session_run)
    sources = [m["source"] for m in mounts]
    targets = [m["target"] for m in mounts]
    assert len(set(sources)) == len(sources) and len(set(targets)) == len(targets)
    for a in p.agents:
        # an agent's home is its own volume, and no other agent's launch uses that home
        assert a["launch"] == ["/opt/asb/asb-agent", a["name"]]
        others = [b for b in p.agents if b["name"] != a["name"]]
        assert all(b["home"] != a["home"] and b["volume"] != a["volume"] for b in others)


def test_config_agent_named_mise_has_a_distinct_agent_volume(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nagents:\n  mise: {command: [mise]}\n")
    mounts = {m["target"]: m["source"] for m in volume_mounts(make_plan(workspace).session_run)}
    assert mounts["/agents/mise"] != mounts["/mise"]


def test_agent_volume_names_do_not_depend_on_the_workspace(home):
    a, b = home / "one" / "api", home / "two" / "web"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    va = {m["source"] for m in volume_mounts(make_plan(a).session_run) if "kanban" not in m["source"]}
    vb = {m["source"] for m in volume_mounts(make_plan(b).session_run) if "kanban" not in m["source"]}
    assert va == vb  # the login is shared across Workspaces; only the Kanban volume is per Workspace (ADR-0020)


def test_mise_has_one_shared_cache_volume_that_is_not_an_agent_home(workspace):
    p = make_plan(workspace)
    mise = [m for m in volume_mounts(p.session_run) if m["source"] == "agent-sandbox-mise"]
    assert [m["target"] for m in mise] == ["/mise"]
    assert all(a["home"] != "/mise" and a["volume"] != "agent-sandbox-mise" for a in p.agents)


def test_nothing_from_the_host_is_mounted_except_the_workspace(workspace, home):
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()
    p = make_plan(workspace)
    binds = [m for m in mount_specs(p.session_run) if m["type"] == "bind"]
    assert [m["source"] for m in binds] == [str(workspace.resolve())]
    assert not any(a in {"-v", "--volume", "--volumes-from"} for a in p.session_run)
    assert str(home) not in " ".join(a for a in p.session_run if a != str(workspace.resolve()) and not a.startswith("agent-sandbox.workspace=")
                                     and str(workspace.resolve()) not in a)


def test_agent_launch_uses_the_configured_command_with_its_own_home(workspace):
    p = make_plan(workspace)
    by_name = {a["name"]: a for a in p.agents}
    assert set(by_name) == set(DEFAULT_AGENTS)
    assert by_name["claude"]["launch"] == ["/opt/asb/asb-agent", "claude"]
    assert by_name["claude"]["command"] == ["claude"]
    assert by_name["claude"]["volume"] == "agent-sandbox-claude"


def test_check_never_outputs_configured_agent_env_values(workspace, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nagents:\n  hermes: {command: [hermes, chat], env: {DEMO_TOKEN: fixture-value}}\n"
    )
    for flags in ([], ["--json"]):
        assert main(["check", str(workspace), *flags], docker=docker.command) == 0
        assert "fixture-value" not in capsys.readouterr().out
    assert docker.calls == []


def test_up_transfers_agent_config_without_values_in_host_argv(workspace, docker, capsys, monkeypatch):
    import subprocess

    docker.has_image()
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nagents:\n  hermes: {command: [hermes, chat], env: {DEMO_TOKEN: fixture-value}}\n"
    )
    real_run = subprocess.run
    seen = []

    def record(argv, *args, **kwargs):
        if kwargs.get("input"):
            seen.append(json.loads(kwargs["input"]))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    assert main(["up", str(workspace)], docker=docker.command) == 0
    assert "fixture-value" not in json.dumps(docker.calls)
    assert [x["hermes"]["env"]["DEMO_TOKEN"] for x in seen] == ["fixture-value"]


def test_a_config_defined_agent_gets_its_own_volume(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nagents:\n  aider: {command: [aider]}\n")
    p = make_plan(workspace)
    by_source = {m["source"]: m["target"] for m in volume_mounts(p.session_run)}
    assert by_source["agent-sandbox-agent.aider"] == "/agents/aider"
    # not baked into the image, so its home may not be writable: say so instead of failing mysteriously
    assert any("aider" in w and "image" in w for w in p.warnings)
    assert not any("claude" in w and "image" in w for w in p.warnings)


def test_volumes_are_listed_in_the_plan_and_survive_json(workspace):
    p = make_plan(workspace)
    data = json.loads(json.dumps(p.to_dict()))
    assert data["volumes"] == sorted({m["source"] for m in volume_mounts(p.session_run)})
    assert "agent-sandbox-mise" in data["volumes"]


def test_session_home_stays_a_tmpfs_not_an_agent_volume(workspace):
    p = make_plan(workspace)
    envs = [p.session_run[i + 1] for i, a in enumerate(p.session_run) if a == "--env"]
    assert "HOME=/tmp" in envs


def test_check_output_lists_agents_and_their_volumes(workspace, docker, capsys):
    assert main(["check", str(workspace)], docker=docker.command) == 0
    out = capsys.readouterr().out
    assert "agent-sandbox-claude" in out and "agent-sandbox-mise" in out
    assert docker.calls == []


# ------------------------------------------------------------------ runtime


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_up_creates_labelled_volumes_before_the_session_and_never_removes_them(workspace, docker, capsys):
    docker.has_image()
    code, _, _ = run(["up", str(workspace)], docker, capsys)
    assert code == 0
    calls = docker.calls
    created = [c for c in calls if c[:2] == ["volume", "create"]]
    assert {c[-1] for c in created} >= {"agent-sandbox-mise", *(f"agent-sandbox-{n}" for n in DEFAULT_AGENTS)}
    assert all("agent-sandbox=volume" in c for c in created)
    first_run = next(i for i, c in enumerate(calls) if c[0] == "run")
    assert all(calls.index(c) < first_run for c in created)
    run(["down", str(workspace)], docker, capsys)
    for c in docker.calls:
        assert c[:2] not in (["volume", "rm"], ["volume", "prune"]) and "-v" not in c


def test_a_failed_volume_create_fails_up_and_starts_nothing(workspace, docker, capsys, monkeypatch):
    docker.has_image()
    # the fake daemon rejects `volume create` when the name starts with this marker
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nagents:\n  boom: {command: [boom]}\n")
    monkeypatch.setenv("FAKE_DOCKER_FAIL_VOLUME", "agent-sandbox-agent.boom")
    code, _, err = run(["up", str(workspace)], docker, capsys)
    assert code != 0 and "agent-sandbox-agent.boom" in err
    assert docker.containers == {}


# ------------------------------------------------------------------ the image definition


def dockerfile():
    return (IMAGE_CONTEXT / "Dockerfile").read_text()


def test_dockerfile_ends_as_a_non_root_user_and_never_copies_host_agent_config():
    text = dockerfile()
    users = [l.split()[1] for l in text.splitlines() if l.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
    assert "COPY" not in "".join(l for l in text.splitlines() if ".claude" in l or ".codex" in l or ".hermes" in l)
    assert "docker.sock" not in text


def test_dockerfile_installs_every_agent_tool_and_mise_defaults():
    text = dockerfile()
    for needle in ("claude-code", "@openai/codex", "oh-my-pi", "hermes", "@dbx-app/mcp-server",
                   "@usebruno/cli", "ripgrep", "jq", "mise"):
        assert needle in text, needle
    for lang in ("node", "python", "go"):
        assert f"{lang}@" in text, lang


def test_dockerfile_points_mise_at_the_volume_and_has_a_home_for_each_default_agent():
    text = dockerfile()
    assert "ENV" in text and "MISE_DATA_DIR=/mise" in text
    # pre-created, world-writable, so a new empty volume inherits a home any --user can write
    line = next(l for l in text.splitlines() if 'mkdir -p -m 0777 "/agents/$a"' in l)
    for name in DEFAULT_AGENTS:
        assert name in line


def test_agent_image_bootstraps_from_verified_inputs():
    text = dockerfile()
    assert "https://mise.run" not in text
    assert "astral.sh/uv/install.sh" not in text
    assert "plannotator.ai/install.sh" not in text
    assert "releases/latest/download" not in text
    assert "sha256sum --check --strict" in (IMAGE_CONTEXT / "fetch-verified.sh").read_text()
    assert "rev-parse HEAD" in text and "uv sync --locked" in text
    assert "pnpm install --frozen-lockfile" in text


def test_verified_fetch_rejects_wrong_digest_and_preserves_destination(tmp_path):
    import subprocess

    dest = tmp_path / "artifact"
    dest.write_text("existing")
    # Substitute only the download; execute the actual verification logic even under noexec /tmp.
    args = ["sh", "-c", 'curl() { while [ "$1" != -o ]; do shift; done; printf fixture > "$2"; }; . "$0"',
            str(IMAGE_CONTEXT / "fetch-verified.sh"), "https://example.invalid/artifact"]
    r = subprocess.run([*args, "0" * 64, str(dest)], capture_output=True, text=True)
    assert r.returncode != 0 and "FAILED" in r.stdout
    assert dest.read_text() == "existing"

    from hashlib import sha256

    digest = sha256(b"fixture").hexdigest()
    subprocess.run([*args, digest, str(dest)], capture_output=True, check=True)
    assert dest.read_bytes() == b"fixture"


def test_image_context_files_exist():
    assert (IMAGE_CONTEXT / "Dockerfile").is_file()
    for f in Path(IMAGE_CONTEXT).glob("*.sh"):
        assert f.read_text().startswith("#!/bin/sh") or f.read_text().startswith("#!/usr/bin/env sh")


def test_wrap_node_bins_pins_node_shebang_scripts_to_the_given_node(tmp_path):
    """The only image script that runs without Docker: check what it actually writes."""
    import shutil
    import subprocess

    src, out = tmp_path / "bin", tmp_path / "wrapped"
    src.mkdir()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cli = pkg / "cli.js"
    cli.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
    cli.chmod(0o755)
    (src / "agent").symlink_to(cli)  # npm installs bins as symlinks into the package
    native = src / "native"
    native.write_text("#!/bin/sh\necho native\n")
    native.chmod(0o755)
    fake_node = shutil.which("echo")  # "runs" the script by printing its path; works under noexec /tmp
    # run through `sh` so the test also works where /tmp is mounted noexec
    subprocess.run(["sh", str(IMAGE_CONTEXT / "wrap-node-bins.sh"), str(src), str(out), fake_node], check=True)

    assert sorted(p.name for p in out.iterdir()) == ["agent"]  # the sh script is not wrapped
    r = subprocess.run(["sh", str(out / "agent")], capture_output=True, text=True)
    assert r.stdout.strip() == str(cli)  # handed to the pinned node, not to PATH's
