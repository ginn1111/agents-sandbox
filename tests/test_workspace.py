import os
from pathlib import Path

import pytest

from agent_sandbox.workspace import WorkspaceError, resolve_workspace


def test_resolves_to_canonical_absolute_path(home):
    project = home / "work" / "api"
    project.mkdir(parents=True)
    link = home / "link"
    link.symlink_to(project)
    assert resolve_workspace(link, home=home) == project.resolve()


def test_defaults_to_current_directory(home, monkeypatch):
    project = home / "work"
    project.mkdir()
    monkeypatch.chdir(project)
    assert resolve_workspace(None, home=home) == project.resolve()


def test_path_with_spaces_is_accepted(home):
    project = home / "my project (v2)"
    project.mkdir()
    assert resolve_workspace(project, home=home) == project.resolve()


def test_refuses_filesystem_root(home):
    with pytest.raises(WorkspaceError, match="filesystem root"):
        resolve_workspace(Path("/"), home=home)


def test_refuses_home_directory_itself(home):
    with pytest.raises(WorkspaceError, match="home directory"):
        resolve_workspace(home, home=home)


def test_refuses_any_path_above_home(home):
    with pytest.raises(WorkspaceError, match="above"):
        resolve_workspace(home.parent, home=home)


def test_refuses_symlink_that_resolves_to_home(home, tmp_path):
    sneaky = tmp_path / "sneaky"
    sneaky.symlink_to(home)
    with pytest.raises(WorkspaceError, match="home directory"):
        resolve_workspace(sneaky, home=home)


def test_refuses_missing_directory(home):
    with pytest.raises(WorkspaceError, match="does not exist"):
        resolve_workspace(home / "nope", home=home)


def test_refuses_a_file(home):
    f = home / "file.txt"
    f.write_text("x")
    with pytest.raises(WorkspaceError, match="not a directory"):
        resolve_workspace(f, home=home)
