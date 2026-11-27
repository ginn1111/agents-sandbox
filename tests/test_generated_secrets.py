"""Seam 1: `{generated}` Portal values (ADR-0010, ADR-0020): created once per Workspace, kept outside it."""
import json
import os
import stat

import pytest

from agent_sandbox import generated
from agent_sandbox.portals import PortalError

ENV = {
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "{generated}",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "{generated}",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "{generated}",
    "PLAIN": "literal",
}


def resolve(ws, session="asb-x-1", portal="hermes-dashboard", env=ENV):
    return generated.resolve(session, ws, portal, env)


def test_literal_values_pass_through_and_generated_ones_are_replaced(workspace):
    out = resolve(workspace)
    assert out["PLAIN"] == "literal"
    for k in ENV:
        if k != "PLAIN":
            assert out[k] != "{generated}" and len(out[k]) >= 32


def test_every_generated_value_is_different(workspace):
    out = resolve(workspace)
    vals = [v for k, v in out.items() if k != "PLAIN"]
    assert len(set(vals)) == len(vals)


def test_values_are_created_once_and_stay_the_same(workspace):
    assert resolve(workspace) == resolve(workspace)


def test_values_persist_in_a_file_outside_the_workspace_that_only_the_user_can_read(workspace, home):
    out = resolve(workspace)
    path = generated.store_path("asb-x-1")
    assert path.is_file() and workspace.resolve() not in path.resolve().parents
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert out["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] in path.read_text()


def test_nothing_is_written_into_the_workspace(workspace):
    resolve(workspace)
    assert list(workspace.rglob("*")) == []


def test_each_workspace_session_has_its_own_values(workspace):
    a = resolve(workspace, session="asb-a-1")
    b = resolve(workspace, session="asb-b-2")
    assert a["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] != b["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"]


def test_each_portal_has_its_own_values(workspace):
    a = resolve(workspace, portal="one")
    b = resolve(workspace, portal="two")
    assert a["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] != b["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"]


def test_a_new_generated_key_is_added_without_changing_the_old_ones(workspace):
    first = resolve(workspace, env={"A": "{generated}"})
    second = resolve(workspace, env={"A": "{generated}", "B": "{generated}"})
    assert second["A"] == first["A"] and second["B"] != second["A"]


def test_a_state_directory_inside_the_workspace_is_refused(workspace, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(workspace / ".state"))
    with pytest.raises(PortalError, match="inside the Workspace"):
        resolve(workspace)
    assert not (workspace / ".state").exists()


def test_a_corrupt_store_is_refused_not_regenerated(workspace):
    resolve(workspace)
    generated.store_path("asb-x-1").write_text("{not json")
    with pytest.raises(PortalError, match="not valid"):
        resolve(workspace)


def test_a_store_that_others_can_read_is_refused(workspace):
    resolve(workspace)
    os.chmod(generated.store_path("asb-x-1"), 0o644)
    with pytest.raises(PortalError, match="permissions"):
        resolve(workspace)


def test_names_lists_the_generated_keys_without_reading_anything(workspace):
    assert generated.names(ENV) == [k for k in ENV if k != "PLAIN"]
    assert generated.names({"A": "x"}) == []


def test_store_file_is_json_keyed_by_portal_and_variable(workspace):
    resolve(workspace)
    data = json.loads(generated.store_path("asb-x-1").read_text())
    assert set(data["hermes-dashboard"]) == {k for k in ENV if k != "PLAIN"}


def test_a_symlinked_store_is_refused(workspace, tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    os.chmod(target, 0o600)
    path = generated.store_path("asb-x-1")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.symlink(target, path)
    with pytest.raises(PortalError, match="permissions"):
        resolve(workspace)


def test_write_refuses_symlinked_tmp_file(workspace, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    path = generated.store_path("asb-x-1")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.symlink(victim, path.with_name(path.name + ".tmp"))
    with pytest.raises(OSError):
        resolve(workspace)
    assert victim.read_text() == "keep"
