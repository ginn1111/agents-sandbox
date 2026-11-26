import json

from agent_sandbox.config import load_config
from agent_sandbox.plan import plan


def make_plan(ws, **cli):
    return plan(ws, load_config(ws, cli=cli or None))


def flag_values(args, flag):
    """All values given for `--flag value` or `--flag=value`."""
    out = []
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith(flag + "="):
            out.append(a.split("=", 1)[1])
    return out


def test_session_is_hardened(workspace):
    args = make_plan(workspace).session_run
    assert args[:2] == ["run", "-d"]
    assert "--read-only" in args
    assert flag_values(args, "--cap-drop") == ["ALL"]
    assert "no-new-privileges" in flag_values(args, "--security-opt")
    assert sorted(flag_values(args, "--tmpfs")) == ["/run", "/tmp"]
    user = flag_values(args, "--user")[0]
    assert user.split(":")[0] not in ("0", "root")


def test_session_has_no_host_control_paths(workspace):
    args = make_plan(workspace).session_run
    flags = {a.split("=", 1)[0] for a in args if a.startswith("-")}
    for forbidden in ("--privileged", "--pid", "--ipc", "--device", "--cap-add",
                      "--volumes-from", "--env-file", "-v", "--volume", "--userns"):
        assert forbidden not in flags
    joined = " ".join(args)
    assert "docker.sock" not in joined and "SSH_AUTH_SOCK" not in joined


def test_no_host_environment_is_passed(workspace, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    args = make_plan(workspace).session_run
    assert "hunter2" not in " ".join(args)
    assert all(not e.startswith("AWS_") for e in flag_values(args, "--env"))


def test_workspace_is_the_only_mount_and_is_read_write(workspace):
    p = make_plan(workspace)
    mounts = flag_values(p.session_run, "--mount")
    assert len(mounts) == 1
    assert f"source={workspace.resolve()}" in mounts[0]
    assert "target=/workspace" in mounts[0]
    assert "readonly" not in mounts[0]
    assert "-v" not in p.session_run and "--volume" not in p.session_run
    assert flag_values(p.session_run, "-w") == ["/workspace"]


def test_resource_limits_come_from_config(workspace):
    args = make_plan(workspace, resources={"cpus": 2, "memory": "1g", "pids_limit": 64}).session_run
    assert flag_values(args, "--cpus") == ["2"]
    assert flag_values(args, "--memory") == ["1g"]
    assert flag_values(args, "--pids-limit") == ["64"]


def test_session_network_is_netguards_never_the_default_bridge(workspace):
    p = make_plan(workspace)
    assert flag_values(p.session_run, "--network") == [f"container:{p.netguard_name}"]
    assert flag_values(make_plan(workspace, offline=True).session_run, "--network") == ["none"]


def test_plan_no_longer_claims_masking_is_missing(workspace):
    p = make_plan(workspace)
    assert not any("not masked yet" in w.lower() for w in p.warnings)


def test_names_are_stable_and_distinct_per_workspace(home):
    a = home / "a" / "api"
    b = home / "b" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    pa1, pa2, pb = make_plan(a), make_plan(a), make_plan(b)
    assert pa1.container_name == pa2.container_name
    assert pa1.container_name != pb.container_name
    assert flag_values(pa1.session_run, "--name") == [pa1.container_name]


def test_session_is_labelled_for_ls(workspace):
    p = make_plan(workspace)
    labels = flag_values(p.session_run, "--label")
    assert "agent-sandbox=session" in labels
    assert f"agent-sandbox.workspace={workspace.resolve()}" in labels


def test_workspace_path_with_comma_stays_one_mount_argument(home):
    ws = home / "a,b"
    ws.mkdir()
    mounts = flag_values(make_plan(ws).session_run, "--mount")
    assert len(mounts) == 1
    assert f'"source={ws.resolve()}"' in mounts[0]  # CSV-quoted field, still one argument


def test_plan_is_json_serialisable_and_lists_limits(workspace):
    p = make_plan(workspace)
    data = json.loads(json.dumps(p.to_dict()))
    assert data["workspace"] == str(workspace.resolve())
    assert data["session_run"] == p.session_run
    assert any("filename" in l for l in data["limits"])
    assert data["result"] == "Ready"


def test_plan_is_pure_and_touches_nothing(workspace):
    before = sorted(p.name for p in workspace.iterdir())
    make_plan(workspace)
    assert sorted(p.name for p in workspace.iterdir()) == before


def test_external_git_metadata_is_warned_not_mounted(workspace):
    (workspace / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")
    p = make_plan(workspace)
    assert any("outside the Workspace" in w for w in p.warnings)
    assert len(flag_values(p.session_run, "--mount")) == 1


def test_project_config_rules_are_listed(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nsecrets: {allow: ['**/.env.local']}\n")
    p = make_plan(workspace)
    assert ["secrets.allow", "**/.env.local"] in [list(r) for r in p.project_rules]
    assert str(workspace / ".agent-sandbox.yaml") in p.config_files
