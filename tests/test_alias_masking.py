"""Ticket #4: every alias of a Secret file is masked, and `up` refuses paths Docker might not resolve (ADR-0004)."""
import os

import pytest

from agent_sandbox.cli import main
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan


def make_plan(ws):
    return plan(ws, load_config(ws))


def masked(p):
    return {e.path: e.reason for e in p.entries if e.status == "MASKED"}


def touch(ws, rel, text="x"):
    f = ws / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def mount_targets(p):
    a = p.session_run
    return [a[i + 1] for i, x in enumerate(a) if x == "--mount"]


def up(ws, docker, capsys):
    code = main(["up", str(ws)], docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------- every alias, in one workspace


@pytest.fixture
def aliased(workspace):
    touch(workspace, ".env", "TOKEN=abc")
    os.link(workspace / ".env", workspace / "notes.txt")                       # hard link
    touch(workspace, "config/real.secretfile")
    os.symlink("config/real.secretfile", workspace / "secrets.json")           # secret-named symlink
    os.link(workspace / "config" / "real.secretfile", workspace / "twin.txt")  # hard link to the symlink's target
    return workspace


def test_the_plan_masks_every_alias_of_every_secret_file(aliased):
    assert set(masked(make_plan(aliased))) == {".env", "notes.txt", "config/real.secretfile", "twin.txt"}


def test_each_alias_gets_its_own_readonly_mount_at_its_real_path(aliased):
    p = make_plan(aliased)
    targets = {t.split("target=")[1].split(",")[0] for t in mount_targets(p)}
    for rel in masked(p):
        assert f"/workspace/{rel}" in targets
    assert "/workspace/secrets.json" not in targets  # the symlink itself is never a mount destination


def test_check_explains_each_alias(aliased, docker, capsys):
    main(["check", str(aliased)], docker=docker.command)
    out = capsys.readouterr().out
    assert any("notes.txt" in l and "hard link to .env" in l for l in out.splitlines())
    assert any("config/real.secretfile" in l and "target of symlink secrets.json" in l for l in out.splitlines())


def test_symlink_inside_a_subdirectory_to_a_sibling_directory_masks_the_real_file(workspace):
    touch(workspace, "store/real")
    (workspace / "app").mkdir()
    os.symlink("../store/real", workspace / "app" / ".env")
    assert masked(make_plan(workspace)) == {"store/real": "target of symlink app/.env"}


def test_secret_symlink_through_a_symlinked_directory_masks_the_real_path_not_the_alias(workspace):
    touch(workspace, "realdir/key")
    os.symlink("realdir", workspace / "linkdir")
    os.symlink("linkdir/key", workspace / ".env")
    assert list(masked(make_plan(workspace))) == ["realdir/key"]


def test_hard_link_to_a_symlinks_target_found_inside_a_masked_directory(workspace):
    touch(workspace, ".ssh/id_rsa")
    os.link(workspace / ".ssh" / "id_rsa", workspace / "copy")
    assert masked(make_plan(workspace))["copy"] == "hard link to .ssh/id_rsa"


# ---------------------------------------------------------------- never follow a link out


def test_secret_symlink_to_outside_masks_nothing_and_reads_no_outside_names(workspace, home):
    outside = home / "outside"
    touch(outside, "dir/inner.env")
    os.symlink(outside / "dir", workspace / ".ssh")
    p = make_plan(workspace)
    assert masked(p) == {}
    assert any(".ssh" in w and "outside the Workspace" in w for w in p.warnings)
    assert not any("target=/workspace/" in a for a in mount_targets(p))


def test_symlink_inside_a_masked_directory_pointing_outside_is_not_followed(workspace, home):
    touch(workspace, ".ssh/a")
    touch(home / "outside", "s")
    os.symlink(home / "outside" / "s", workspace / ".ssh" / "out")
    p = make_plan(workspace)
    assert list(masked(p)) == [".ssh"]


def test_hard_link_outside_the_workspace_is_not_found_or_followed(workspace, home):
    touch(workspace, ".env")
    os.link(workspace / ".env", home / "elsewhere.txt")
    assert list(masked(make_plan(workspace))) == [".env"]


# ---------------------------------------------------------------- Docker may not resolve symlinked destinations


def started(ws, docker, capsys):
    docker.has_image()
    return up(ws, docker, capsys)


def test_up_refuses_when_a_masked_path_runs_through_a_symlinked_directory(workspace, docker, capsys):
    touch(workspace, "config/real.env")
    os.symlink("config/real.env", workspace / ".env")
    p = make_plan(workspace)
    assert list(masked(p)) == ["config/real.env"]
    # between plan and up, `config` becomes a symlink: Docker could resolve the destination elsewhere
    from agent_sandbox.masks import MaskError, check_originals_exist
    os.rename(workspace / "config", workspace / "config2")
    os.symlink("config2", workspace / "config")
    with pytest.raises(MaskError, match=r"config.*symlink"):
        check_originals_exist(p)


def test_up_refuses_before_touching_docker_run_when_a_mask_path_became_a_symlinked_directory(workspace, docker, capsys, monkeypatch):
    touch(workspace, "config/real.env")
    os.symlink("config/real.env", workspace / ".env")
    docker.has_image()
    from agent_sandbox import runtime

    real = runtime.check_originals_exist

    def swap(p):
        os.rename(workspace / "config", workspace / "config2")
        os.symlink("config2", workspace / "config")
        return real(p)

    monkeypatch.setattr(runtime, "check_originals_exist", swap)
    code, _, err = up(workspace, docker, capsys)
    assert code != 0 and "symlink" in err
    assert not any(c[0] == "run" for c in docker.calls)
    assert docker.containers == {}


def test_a_plain_nested_mask_path_is_still_accepted(workspace, docker, capsys):
    touch(workspace, "a/b/.env")
    code, _, err = started(workspace, docker, capsys)
    assert code == 0, err


# ---------------------------------------------------------------- attach warns about files added after up


def attach(ws, docker, capsys):
    code = main(["attach", str(ws)], docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_attach_warns_listing_secret_files_that_appeared_after_up(workspace, docker, capsys):
    touch(workspace, ".env")
    started(workspace, docker, capsys)
    touch(workspace, "late/.env.local")
    touch(workspace, "id_rsa")
    os.link(workspace / ".env", workspace / "late-alias.txt")
    code, _, err = attach(workspace, docker, capsys)
    assert code == 0
    assert "NOT masked" in err
    for rel in ("late/.env.local", "id_rsa", "late-alias.txt"):
        assert rel in err
    assert ".env," not in err and err.count("late-alias.txt") == 1


def test_attach_still_opens_herdr_after_warning(workspace, docker, capsys):
    touch(workspace, ".env")
    started(workspace, docker, capsys)
    touch(workspace, ".env.local")
    attach(workspace, docker, capsys)
    assert docker.calls[-1][0] == "exec" and "herdr" in docker.calls[-1]


def test_attach_is_quiet_when_nothing_new_appeared(workspace, docker, capsys):
    touch(workspace, ".env")
    touch(workspace, ".ssh/id_rsa")
    started(workspace, docker, capsys)
    touch(workspace, "ordinary.txt")
    touch(workspace, ".ssh/another")  # under a masked directory: already covered
    code, _, err = attach(workspace, docker, capsys)
    assert code == 0 and "warning" not in err.lower()


def test_attach_warns_about_a_new_secret_directory(workspace, docker, capsys):
    touch(workspace, "a.txt")
    started(workspace, docker, capsys)
    touch(workspace, ".ssh/k")
    _, _, err = attach(workspace, docker, capsys)
    assert ".ssh" in err and "NOT masked" in err


def test_attach_that_starts_the_session_has_nothing_to_warn_about(workspace, docker, capsys):
    touch(workspace, ".env")
    docker.has_image()
    _, _, err = attach(workspace, docker, capsys)
    assert "NOT masked" not in err


def test_attach_warns_when_it_cannot_inspect_the_running_session(workspace, docker, capsys):
    touch(workspace, ".env")
    started(workspace, docker, capsys)
    docker.set(inspect_fails=True)
    code, _, err = attach(workspace, docker, capsys)
    assert code == 0 and "cannot check" in err


def test_attach_does_not_modify_the_workspace_while_checking(workspace, docker, capsys):
    touch(workspace, ".env")
    started(workspace, docker, capsys)
    touch(workspace, ".env.local", "LATE=1")
    before = sorted(p.name for p in workspace.iterdir())
    attach(workspace, docker, capsys)
    assert sorted(p.name for p in workspace.iterdir()) == before
    assert (workspace / ".env.local").read_text() == "LATE=1"
