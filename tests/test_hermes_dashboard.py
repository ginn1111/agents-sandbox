"""Seam 1: the built-in Hermes dashboard Portal with a generated login (ticket 09, ADR-0010, ADR-0013, ADR-0020).

Docker is the fake from fake_docker_portals.py. Nothing here proves the dashboard itself answers; that is
tests/test_e2e_hermes_dashboard.py (Seam 2, skipped without Docker) and docs/spikes/hermes-dashboard.md.
"""
import json
import os
import re
import sys

import pytest

from agent_sandbox import generated
from agent_sandbox.config import ConfigError, load_config
from agent_sandbox.plan import plan, session_name

sys.path.insert(0, os.path.dirname(__file__))
from test_portals_cli import PortalDocker, pdocker, run, start, calls  # noqa: E402,F401

DASH = "hermes-dashboard"
USER, PASSWORD, SECRET = (f"HERMES_DASHBOARD_BASIC_AUTH_{k}" for k in ("USERNAME", "PASSWORD", "SECRET"))


def make_plan(ws, yaml_text=None, **cli):
    if yaml_text:
        (ws / ".agent-sandbox.yaml").write_text("version: 1\n" + yaml_text)
    return plan(ws, load_config(ws, cli=cli or None))


def entry(p, name=DASH):
    return next(e for e in p.portals if e["name"] == name)


@pytest.fixture
def up(workspace, pdocker, capsys):
    assert run(["up", str(workspace)], pdocker, capsys)[0] == 0
    return workspace


def login_of(out):
    """(username, password) from what `portal start` printed."""
    m = re.search(r"username:\s*(\S+)\s+password:\s*(\S+)", out)
    assert m, out
    return m.group(1), m.group(2)


# -- the built-in entry

def test_the_hermes_dashboard_is_a_built_in_portal_run_by_the_cli(workspace):
    e = entry(make_plan(workspace))
    assert e["port"] == 9119 and e["open"] == "/" and e["started_by"] == "cli"
    assert e["command"] == ["hermes", "dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"]


def test_the_login_variables_are_generated_never_literal(workspace):
    e = entry(make_plan(workspace))
    for k in (USER, PASSWORD, SECRET):
        assert e["env"][k] == "{generated}"
    assert e["login"] == {"username": USER, "password": PASSWORD}


def test_the_dashboard_serves_the_web_ui_built_into_the_image(workspace):
    # the root filesystem is read-only, so the dashboard cannot build its UI at start (ADR-0020)
    assert entry(make_plan(workspace))["env"]["HERMES_WEB_DIST"] == "/opt/hermes/src/hermes_cli/web_dist"


def test_plain_up_does_not_start_or_publish_the_dashboard(up, pdocker, capsys):
    assert not [c for c in pdocker.calls if "dashboard" in c]
    assert not calls(pdocker, "network", "create")


def test_nothing_of_the_login_is_in_the_session_or_netguard_arguments(workspace):
    p = make_plan(workspace)
    joined = json.dumps([p.session_run, p.netguard_run])
    assert "BASIC_AUTH" not in joined and "{generated}" not in joined


def test_check_and_check_json_never_contain_generated_values(workspace, home, capsys):
    from agent_sandbox.cli import main
    main(["check", str(workspace)])
    text = capsys.readouterr().out
    main(["check", "--json", str(workspace)])
    js = capsys.readouterr().out
    assert generated.store_path(session_name(workspace.resolve())).exists() is False  # `check` writes nothing
    assert "{generated}" in js and DASH in text


def test_generated_values_are_refused_for_a_portal_the_agent_starts(workspace):
    # the Session environment is fixed at `up`, before any secret exists; refuse rather than leak a literal marker
    p = make_plan(workspace, "portals: {web: {port: 8080, env: {TOKEN: '{generated}'}}}\n")
    assert p.result == "Blocked" and any("generated" in e and "web" in e for e in p.errors)


# -- config rules

LOGIN = f"login: {{username: {USER}, password: {PASSWORD}}}"


def test_project_config_cannot_put_a_literal_in_a_login_variable(workspace):
    (workspace / ".agent-sandbox.yaml").write_text(
        f"version: 1\nportals:\n  {DASH}:\n    port: 9119\n    command: [hermes, dashboard]\n"
        f"    env: {{{PASSWORD}: hunter2, {USER}: '{{generated}}'}}\n    {LOGIN}\n")
    with pytest.raises(ConfigError, match="generated"):
        load_config(workspace)


def test_global_config_may_set_a_literal_login(workspace, home):
    g = home / ".config" / "agent-sandbox" / "config.yaml"
    g.parent.mkdir(parents=True)
    g.write_text(f"version: 1\nportals:\n  {DASH}:\n    port: 9119\n    command: [hermes, dashboard]\n"
                 f"    env: {{{PASSWORD}: hunter2, {USER}: me}}\n    {LOGIN}\n")
    assert load_config(workspace).portals[DASH]["env"][PASSWORD] == "hunter2"


def test_project_config_still_cannot_set_portal_secrets_key(workspace):
    (workspace / ".agent-sandbox.yaml").write_text("version: 1\nportals: {p: {port: 80, secrets: {x: y}}}\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(workspace)


def test_project_config_can_still_use_the_generated_marker_for_its_own_portal(workspace):
    p = make_plan(workspace, "portals: {web: {port: 8080, command: [serve], env: {TOKEN: '{generated}'}}}\n")
    assert p.result == "Ready" and entry(p, "web")["env"]["TOKEN"] == "{generated}"


@pytest.mark.parametrize("login", ["{username: NOPE}", "{password: ''}", "{other: X}", "[A]", "{username: 1}"])
def test_login_must_name_env_variables_of_the_portal(workspace, login):
    (workspace / ".agent-sandbox.yaml").write_text(
        f"version: 1\nportals: {{web: {{port: 80, command: [x], env: {{A: '{{generated}}'}}, login: {login}}}}}\n")
    with pytest.raises(ConfigError, match="login"):
        load_config(workspace)


# -- start

def test_start_prints_the_url_and_a_generated_login(up, pdocker, capsys):
    code, out, err = start(up, pdocker, capsys, DASH)
    assert code == 0, err
    assert f"{DASH} → http://127.0.0.1:" in out
    user, password = login_of(out)
    assert len(user) >= 16 and len(password) >= 32
    assert "outside the Workspace" in out


def test_start_never_prints_the_signing_secret(up, pdocker, capsys):
    _, out, err = start(up, pdocker, capsys, DASH)
    secret = generated.resolve(session_name(up.resolve()), up, DASH, entry(make_plan(up))["env"])[SECRET]
    assert secret not in out + err


def test_the_command_gets_the_login_in_its_environment_but_not_on_the_docker_command_line(up, pdocker, capsys):
    _, out, _ = start(up, pdocker, capsys, DASH)
    user, password = login_of(out)
    argv = json.dumps(pdocker.calls)
    assert password not in argv and user not in argv  # `ps` on the host must not show it
    proc = pdocker.state()["procs"][session_name(up.resolve())][f"/tmp/asb-portal-{DASH}.pid"]
    env = dict(e.split("=", 1) for e in proc["env"])
    assert env[PASSWORD] == password and env[USER] == user and len(env[SECRET]) >= 32
    assert env["HERMES_WEB_DIST"] == "/opt/hermes/src/hermes_cli/web_dist"
    assert proc["argv"] == ["hermes", "dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"]


def test_the_login_is_stored_outside_the_workspace_and_nothing_is_written_inside_it(up, pdocker, capsys):
    before = sorted(p.name for p in up.rglob("*"))
    _, out, _ = start(up, pdocker, capsys, DASH)
    _, password = login_of(out)
    path = generated.store_path(session_name(up.resolve()))
    assert password in path.read_text() and up.resolve() not in path.resolve().parents
    assert sorted(p.name for p in up.rglob("*")) == before
    assert str(path) in out


def test_the_same_login_comes_back_after_stop_start_and_down_up(up, pdocker, capsys):
    _, out1, _ = start(up, pdocker, capsys, DASH)
    run(["portal", "stop", DASH, str(up)], pdocker, capsys)
    _, out2, _ = start(up, pdocker, capsys, DASH)
    run(["down", str(up)], pdocker, capsys)
    run(["up", str(up)], pdocker, capsys)
    _, out3, _ = start(up, pdocker, capsys, DASH)
    assert login_of(out1) == login_of(out2) == login_of(out3)


def test_start_again_while_published_prints_the_login_again(up, pdocker, capsys):
    _, out1, _ = start(up, pdocker, capsys, DASH)
    _, out2, _ = start(up, pdocker, capsys, DASH)
    assert login_of(out1) == login_of(out2)


def test_two_workspaces_have_different_logins(home, pdocker, capsys):
    a, b = home / "a" / "api", home / "b" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    logins = []
    for ws in (a, b):
        run(["up", str(ws)], pdocker, capsys)
        logins.append(login_of(start(ws, pdocker, capsys, DASH)[1]))
    assert logins[0][1] != logins[1][1] and logins[0][0] != logins[1][0]


def test_a_secret_store_inside_the_workspace_is_refused_before_anything_starts(up, pdocker, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(up / ".state"))
    code, out, err = start(up, pdocker, capsys, DASH)
    assert code != 0 and "inside the Workspace" in err and "http://" not in out
    assert not calls(pdocker, "network", "create") and not pdocker.state().get("procs", {}).get(session_name(up.resolve()))
    assert not (up / ".state").exists()


def test_status_shows_the_url_but_not_the_password(up, pdocker, capsys):
    _, out, _ = start(up, pdocker, capsys, DASH)
    _, password = login_of(out)
    _, st, _ = run(["portal", "status", DASH, str(up)], pdocker, capsys)
    assert "published at http://127.0.0.1:" in st and password not in st


def test_ls_lists_the_url_but_not_the_password(up, pdocker, capsys):
    _, out, _ = start(up, pdocker, capsys, DASH)
    _, password = login_of(out)
    _, ls, _ = run(["ls"], pdocker, capsys)
    assert f"{DASH} → http://127.0.0.1:" in ls and password not in ls


def test_a_portal_without_a_login_mapping_prints_no_login(up, pdocker, capsys):
    (up / ".agent-sandbox.yaml").write_text(
        "version: 1\nportals: {docs: {port: 8080, command: [serve], env: {K: '{generated}'}}}\n")
    _, out, _ = start(up, pdocker, capsys, "docs")
    assert "docs → http://127.0.0.1:" in out and "password" not in out


def test_stop_stops_the_dashboard_process_and_unpublishes(up, pdocker, capsys):
    start(up, pdocker, capsys, DASH)
    run(["portal", "stop", DASH, str(up)], pdocker, capsys)
    _, st, _ = run(["portal", "status", DASH, str(up)], pdocker, capsys)
    assert "not published" in st and "not running" in st


# -- image (read as text; building needs Docker)

from pathlib import Path  # noqa: E402

DOCKERFILE = (Path(__file__).parent.parent / "src" / "agent_sandbox" / "images" / "agent" / "Dockerfile").read_text()


def test_the_image_installs_hermes_with_its_web_server_packages_and_builds_the_ui():
    assert "--extra web" in DOCKERFILE
    assert "hermes_cli/web_dist" in DOCKERFILE and "scripts/build/web.mjs" in DOCKERFILE
    assert "index.html" in DOCKERFILE  # the build fails loudly when the UI is missing


def test_the_image_has_a_world_writable_kanban_home_so_a_new_volume_is_writable():
    assert any("/kanban" in l and "0777" in l for l in DOCKERFILE.splitlines())
