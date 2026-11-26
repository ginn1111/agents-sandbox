"""Seam 1: Portals in the SessionPlan (ADR-0010, ADR-0013, ADR-0018). No Docker."""
import pytest

from agent_sandbox import portals
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan


def make_plan(ws, yaml_text=None, **cli):
    if yaml_text:
        (ws / ".agent-sandbox.yaml").write_text("version: 1\n" + yaml_text)
    return plan(ws, load_config(ws, cli=cli or None))


def by_name(p):
    return {e["name"]: e for e in p.portals}


def test_plannotator_is_listed_and_is_not_started_by_the_plan(workspace):
    e = by_name(make_plan(workspace))["plannotator"]
    assert e["port"] == 19432 and e["open"] == "/"
    assert e["env"] == {"PLANNOTATOR_REMOTE": "1", "PLANNOTATOR_PORT": "19432"}
    assert e["command"] is None and e["started_by"] == "agent"


def test_publication_is_loopback_only_with_a_host_port_docker_picks(workspace):
    p = make_plan(workspace, "portals: {a: {port: 8080}, b: {port: 9090}}\n")
    for e in p.portals:
        # `127.0.0.1::<port>`: an empty host port means Docker allocates a random free one, never 0.0.0.0
        assert e["publish"] == "127.0.0.1::8000"
        assert not e["publish"].startswith(("0.0.0.0", ":"))


def test_the_publisher_is_a_separate_container_so_the_session_and_netguard_publish_nothing(workspace):
    p = make_plan(workspace)
    for args in (p.session_run, p.netguard_run):
        assert "--publish" not in args and "-p" not in args
        assert not any(a.startswith("--publish=") for a in args)


def test_every_portal_gets_its_own_publisher_and_its_own_network(workspace):
    p = make_plan(workspace, "portals: {a: {port: 8080}}\n")
    es = by_name(p)
    assert es["a"]["publisher"] == f"{p.container_name}-portal-a"
    assert es["plannotator"]["publisher"] == f"{p.container_name}-portal-plannotator"
    assert es["a"]["network"] != es["plannotator"]["network"]
    assert all(e["network"].startswith(p.container_name) for e in p.portals)


def test_two_workspaces_never_share_publisher_or_network_names(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    pa, pb = make_plan(a), make_plan(b)
    assert not {e["publisher"] for e in pa.portals} & {e["publisher"] for e in pb.portals}
    assert not {e["network"] for e in pa.portals} & {e["network"] for e in pb.portals}


def test_a_portal_command_is_managed_by_the_cli_and_without_one_the_agent_starts_it(workspace):
    p = make_plan(workspace, "portals: {docs: {port: 8080, command: [serve, '8080']}}\n")
    es = by_name(p)
    assert es["docs"]["command"] == ["serve", "8080"] and es["docs"]["started_by"] == "cli"
    assert es["plannotator"]["started_by"] == "agent"


def test_portal_env_for_agent_started_portals_is_set_on_the_session_not_netguard(workspace):
    p = make_plan(workspace)
    envs = [p.session_run[i + 1] for i, a in enumerate(p.session_run) if a == "--env"]
    assert "PLANNOTATOR_REMOTE=1" in envs and "PLANNOTATOR_PORT=19432" in envs
    assert not any("PLANNOTATOR" in a for a in p.netguard_run)


def test_env_of_a_cli_started_portal_is_not_baked_into_the_session(workspace):
    p = make_plan(workspace, "portals: {docs: {port: 8080, command: [serve], env: {DOCS_MODE: x}}}\n")
    assert not any("DOCS_MODE" in a for a in p.session_run)


def test_conflicting_env_between_agent_started_portals_blocks_the_plan(workspace):
    p = make_plan(workspace, "portals: {a: {port: 1, env: {X: '1'}}, b: {port: 2, env: {X: '2'}}}\n")
    assert p.result == "Blocked" and any("X" in e for e in p.errors)


def test_offline_sessions_have_no_portals(workspace):
    p = make_plan(workspace, offline=True)
    assert p.portals == [] and any("offline" in w.lower() for w in p.warnings)


def test_plan_serialises_portals(workspace):
    import json
    d = json.loads(json.dumps(make_plan(workspace).to_dict()))
    assert {"plannotator", "hermes-dashboard"} <= {p["name"] for p in d["portals"]}


def test_url_is_loopback_with_the_configured_path():
    assert portals.url(49153, "/") == "http://127.0.0.1:49153/"
    assert portals.url(49153, "/review") == "http://127.0.0.1:49153/review"
    assert portals.url(49153, None) == "http://127.0.0.1:49153/"


def test_docker_port_output_is_accepted_only_when_bound_to_loopback():
    assert portals.parse_published("127.0.0.1:49153\n") == 49153
    for bad in ("0.0.0.0:49153\n", "[::]:49153\n", "127.0.0.1:49153\n0.0.0.0:49153\n", "", "garbage"):
        with pytest.raises(portals.PortalError):
            portals.parse_published(bad)


def test_publisher_run_is_hardened_and_publishes_to_loopback_only(workspace):
    p = make_plan(workspace)
    e = by_name(p)["plannotator"]
    args = portals.publisher_run(e, p.workspace, "172.30.0.2")
    assert args[:2] == ["run", "-d"]
    assert args[args.index("--publish") + 1] == "127.0.0.1::8000"
    assert "--network" not in args  # default bridge; the CLI connects the Portal network afterwards
    assert "--read-only" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in args
    assert args[args.index("--user") + 1].split(":")[0] not in ("0", "root")
    for forbidden in ("--privileged", "--cap-add", "--mount", "-v", "--volume", "--env", "--pid", "--ipc", "--add-host"):
        assert forbidden not in args  # nothing from the host or the Workspace
    assert args[-2:] == ["TCP-LISTEN:8000,fork,reuseaddr", "TCP:172.30.0.2:19432"]
    assert args[-3] == portals.IMAGE


# --- image: Plannotator ships in the agent image (read as text; building needs Docker)

from pathlib import Path

AGENT_IMAGE = Path(__file__).parent.parent / "src" / "agent_sandbox" / "images" / "agent"


def test_the_agent_image_installs_plannotator_binary_only():
    docker = (AGENT_IMAGE / "Dockerfile").read_text()
    assert "backnotprop/plannotator/releases/download/" in docker
    assert "plannotator.ai/install.sh" not in docker
    assert "/opt/plannotator/.local/bin" in docker.split("ENV PATH=")[1].split("\n")[0]


def test_the_claude_plan_hook_is_system_managed_settings_not_a_volume_or_host_file():
    import json
    docker = (AGENT_IMAGE / "Dockerfile").read_text()
    assert "/etc/claude-code/managed-settings.json" in docker
    hook = json.loads((AGENT_IMAGE / "claude-managed-settings.json").read_text())["hooks"]["PermissionRequest"][0]
    assert hook["matcher"] == "ExitPlanMode" and hook["hooks"][0]["command"] == "plannotator"
