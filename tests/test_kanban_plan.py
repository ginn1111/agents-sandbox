"""Seam 1: the per-Workspace Hermes Kanban board in the SessionPlan (ticket 09, ADR-0010, ADR-0020). No Docker."""
import csv
import json
import re

from agent_sandbox.cli import main
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan

# Hermes' own rule for a board slug (hermes_cli/kanban_db.py _BOARD_SLUG_RE).
HERMES_BOARD_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def make_plan(ws, yaml_text=None, **cli):
    if yaml_text:
        (ws / ".agent-sandbox.yaml").write_text("version: 1\n" + yaml_text)
    return plan(ws, load_config(ws, cli=cli or None))


def session_env(p):
    return dict(a.split("=", 1) for i, a in enumerate(p.session_run) if i and p.session_run[i - 1] == "--env")


def volume_mounts(p):
    out = []
    for i, a in enumerate(p.session_run):
        if a == "--mount":
            (row,) = csv.reader([p.session_run[i + 1]])
            spec = dict(f.split("=", 1) for f in row if "=" in f)
            if spec.get("type") == "volume":
                out.append(spec)
    return out


def test_the_session_pins_its_workspace_board(workspace):
    p = make_plan(workspace)
    board = session_env(p)["HERMES_KANBAN_BOARD"]
    assert HERMES_BOARD_SLUG.match(board)
    assert p.kanban["board"] == board
    # the board is named after the Workspace, so the Kanban tab shows which one it is
    assert "my-api" in board


def test_the_board_slug_follows_the_session_name_so_it_is_unique_per_workspace(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert make_plan(a).kanban["board"] != make_plan(b).kanban["board"]


def test_kanban_data_lives_in_a_per_workspace_volume_not_the_shared_hermes_volume(workspace):
    p = make_plan(workspace)
    env = session_env(p)
    home_dir = env["HERMES_KANBAN_HOME"]
    mounts = {m["target"]: m["source"] for m in volume_mounts(p)}
    assert mounts[home_dir] == p.kanban["volume"]
    # the Hermes Agent volume is shared by every Workspace (ADR-0006): the Kanban home is a different mount
    assert p.kanban["volume"] != "agent-sandbox-hermes" and home_dir != "/agents/hermes"
    assert not home_dir.startswith("/agents/") and not home_dir.startswith("/workspace")
    assert p.kanban["volume"] in p.volumes


def test_two_workspaces_never_share_a_kanban_volume(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    pa, pb = make_plan(a), make_plan(b)
    assert pa.kanban["volume"] != pb.kanban["volume"]
    assert not {m["source"] for m in volume_mounts(pa) if "kanban" in m["source"]} & {
        m["source"] for m in volume_mounts(pb) if "kanban" in m["source"]}


def test_the_kanban_mount_is_a_volume_never_a_host_path(workspace):
    p = make_plan(workspace)
    kinds = [m["type"] for m in volume_mounts(p)]
    assert kinds and all(k == "volume" for k in kinds)
    assert "--privileged" not in p.session_run


def test_omp_and_the_other_agents_are_not_wired_to_kanban(workspace):
    p = make_plan(workspace)
    for a in p.agents:
        assert not any(k.startswith("HERMES_KANBAN") for k in a["env"])
    assert "kanban" not in " ".join(p.netguard_run).lower()


def test_the_plan_and_check_json_carry_the_kanban_entry(workspace, capsys):
    main(["check", "--json", str(workspace)])
    d = json.loads(capsys.readouterr().out)
    assert d["kanban"]["board"] and d["kanban"]["volume"] and d["kanban"]["home"]


def test_check_says_where_the_board_lives(workspace, capsys):
    main(["check", str(workspace)])
    out = capsys.readouterr().out
    assert "Kanban" in out and make_plan(workspace).kanban["board"] in out


def test_an_offline_session_still_pins_its_board(workspace):
    p = make_plan(workspace, offline=True)
    assert session_env(p)["HERMES_KANBAN_BOARD"] == p.kanban["board"]
