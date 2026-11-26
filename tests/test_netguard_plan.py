"""Seam 1: how the plan wires Netguard and the Session together (ADR-0003). No Docker."""
from agent_sandbox.config import load_config
from agent_sandbox.netguard import RULES_ENV, rules
from agent_sandbox.plan import plan


def make_plan(ws, **cli):
    return plan(ws, load_config(ws, cli=cli or None))


def flag_values(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def test_session_joins_the_netguard_network(workspace):
    p = make_plan(workspace)
    assert p.netguard_name and p.netguard_name != p.container_name
    assert flag_values(p.session_run, "--network") == [f"container:{p.netguard_name}"]


def test_only_netguard_holds_net_admin(workspace):
    p = make_plan(workspace)
    assert flag_values(p.netguard_run, "--cap-drop") == ["ALL"]
    assert flag_values(p.netguard_run, "--cap-add") == ["NET_ADMIN"]
    assert flag_values(p.session_run, "--cap-drop") == ["ALL"]
    assert "--cap-add" not in p.session_run


def test_netguard_is_hardened_like_the_session(workspace):
    args = make_plan(workspace).netguard_run
    assert args[:2] == ["run", "-d"]
    assert "--read-only" in args
    assert "no-new-privileges" in flag_values(args, "--security-opt")
    assert "/run" in flag_values(args, "--tmpfs")
    for forbidden in ("--privileged", "--pid", "--ipc", "--device", "--volume", "-v", "--mount",
                      "--env-file", "--volumes-from", "--network", "-p", "--publish"):
        assert forbidden not in args, forbidden
    assert "docker.sock" not in " ".join(args)


def test_ipv6_is_disabled_in_the_shared_network(workspace):
    sysctls = flag_values(make_plan(workspace).netguard_run, "--sysctl")
    assert "net.ipv6.conf.all.disable_ipv6=1" in sysctls
    assert "net.ipv6.conf.default.disable_ipv6=1" in sysctls


def test_host_gateway_name_and_hostname_belong_to_netguard_not_the_session(workspace):
    p = make_plan(workspace)
    assert flag_values(p.netguard_run, "--add-host") == ["host.docker.internal:host-gateway"]
    # Docker rejects these on a container that joins another's network namespace
    for flag in ("--add-host", "--hostname", "--dns", "-p", "--publish", "--sysctl"):
        assert flag not in p.session_run, flag


def test_netguard_receives_the_generated_rules(workspace):
    p = make_plan(workspace)
    (env,) = [e for e in flag_values(p.netguard_run, "--env") if e.startswith(RULES_ENV + "=")]
    assert env.split("=", 1)[1].split(";") == rules()
    assert p.netguard_rules == rules()


def test_no_host_environment_reaches_netguard(workspace, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hunter2")
    assert "hunter2" not in " ".join(make_plan(workspace).netguard_run)


def test_netguard_is_labelled_for_down_and_not_listed_as_a_session(workspace):
    p = make_plan(workspace)
    labels = flag_values(p.netguard_run, "--label")
    assert "agent-sandbox=netguard" in labels
    assert f"agent-sandbox.workspace={workspace.resolve()}" in labels
    assert flag_values(p.netguard_run, "--name") == [p.netguard_name]


def test_netguard_image_is_the_last_argument_and_is_the_netguard_image(workspace):
    p = make_plan(workspace)
    assert p.netguard_run[-1] == p.netguard_image == "agent-sandbox-netguard:latest"


def test_netguard_has_small_resource_limits(workspace):
    args = make_plan(workspace).netguard_run
    assert flag_values(args, "--memory") and flag_values(args, "--pids-limit")


def test_each_workspace_gets_its_own_netguard(home):
    a, b = home / "a" / "api", home / "b" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    pa, pb = make_plan(a), make_plan(b)
    assert pa.netguard_name != pb.netguard_name
    assert flag_values(pa.session_run, "--network") != flag_values(pb.session_run, "--network")


def test_offline_session_has_no_network_and_no_netguard(workspace):
    p = make_plan(workspace, offline=True)
    assert flag_values(p.session_run, "--network") == ["none"]
    assert p.netguard_run is None and p.netguard_name is None and p.netguard_rules == []
    assert not any("No Netguard" in w for w in p.warnings)


def test_default_plan_no_longer_warns_about_a_missing_netguard(workspace):
    assert not any("No Netguard" in w for w in make_plan(workspace).warnings)


def test_plan_states_the_network_limit_it_cannot_cover(workspace):
    assert any("public IP" in l for l in make_plan(workspace).limits)


def test_check_json_carries_the_netguard_plan(workspace):
    d = make_plan(workspace).to_dict()
    assert d["netguard_run"][0] == "run" and d["netguard_rules"][0] == "*filter"


def test_host_gateway_name_is_only_added_on_linux(workspace):
    cfg = load_config(workspace)
    assert flag_values(plan(workspace, cfg, platform="linux").netguard_run, "--add-host") == [
        "host.docker.internal:host-gateway"]
    assert flag_values(plan(workspace, cfg, platform="darwin").netguard_run, "--add-host") == []


def test_check_output_states_the_network_mode(workspace):
    from agent_sandbox.render import render
    assert "Netguard" in render(make_plan(workspace))
    assert "offline" in render(make_plan(workspace, offline=True)).lower()
