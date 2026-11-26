"""How the plan turns MASKED entries into Session mounts, and how `up` fails closed (ADR-0004, SPEC 5)."""
import csv
import os
import stat

import pytest

from agent_sandbox.cli import main
from agent_sandbox.config import load_config
from agent_sandbox.plan import plan


def make_plan(ws):
    return plan(ws, load_config(ws))


def parse_mount(arg):
    """Parse a docker --mount value (CSV fields) into a dict."""
    fields = next(csv.reader([arg]))
    return {k: v for k, _, v in (f.partition("=") for f in fields)}


def mounts(p):
    args = p.session_run
    return [parse_mount(args[i + 1]) for i, a in enumerate(args) if a == "--mount"]


def mask_mounts(p):
    return {m["target"]: m for m in mounts(p) if m["target"] != "/workspace"}


def touch(ws, rel, text="x"):
    f = ws / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


# ------------------------------------------------------------------ plan


def test_each_masked_file_is_a_readonly_bind_mount_of_the_empty_file_at_its_workspace_path(workspace):
    touch(workspace, ".env")
    touch(workspace, "api/.env.local")
    p = make_plan(workspace)
    mm = mask_mounts(p)
    assert set(mm) == {"/workspace/.env", "/workspace/api/.env.local"}
    for m in mm.values():
        assert m["type"] == "bind" and "readonly" in m
        assert m["source"] == p.mask_sources["file"]


def test_masked_directories_mount_an_empty_readonly_directory(workspace):
    touch(workspace, ".ssh/id_rsa")
    p = make_plan(workspace)
    m = mask_mounts(p)["/workspace/.ssh"]
    assert m["source"] == p.mask_sources["dir"] and "readonly" in m
    assert set(p.mask_sources) == {"dir"}  # no file masks, so no empty-file source


def test_mask_sources_are_never_inside_the_workspace(workspace):
    touch(workspace, ".env")
    for src in make_plan(workspace).mask_sources.values():
        assert not os.path.realpath(src).startswith(str(workspace.resolve()) + os.sep)


def test_symlink_secret_is_masked_at_the_resolved_real_path(workspace):
    touch(workspace, "config/real.env")
    os.symlink("config/real.env", workspace / ".env")
    assert set(mask_mounts(make_plan(workspace))) == {"/workspace/config/real.env"}


def test_workspace_mount_stays_read_write_and_masks_add_only_readonly_mounts(workspace):
    touch(workspace, ".env")
    ms = mounts(make_plan(workspace))
    ws = [m for m in ms if m["target"] == "/workspace"]
    assert len(ws) == 1 and "readonly" not in ws[0]
    assert all("readonly" in m for m in ms if m["target"] != "/workspace")
    assert ms[0]["target"] == "/workspace"


def test_no_masks_means_no_mask_sources_and_only_the_workspace_mount(workspace):
    touch(workspace, "README.md")
    p = make_plan(workspace)
    assert p.mask_sources == {} and len(mounts(p)) == 1


def test_paths_with_commas_quotes_and_spaces_stay_one_mount_argument_each(workspace):
    rel = 'we ird,"dir"/.env'
    touch(workspace, rel)
    p = make_plan(workspace)
    assert set(mask_mounts(p)) == {"/workspace/" + rel}
    assert [a for a in p.session_run if a == "--mount"].__len__() == 2


def test_hard_link_aliases_get_their_own_mounts(workspace):
    touch(workspace, ".env")
    os.link(workspace / ".env", workspace / "notes.txt")
    assert set(mask_mounts(make_plan(workspace))) == {"/workspace/.env", "/workspace/notes.txt"}


def test_mask_sources_live_under_xdg_state_home_when_set(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    touch(workspace, ".env")
    assert make_plan(workspace).mask_sources["file"].startswith(str(tmp_path / "state"))


def test_a_mask_source_inside_the_workspace_blocks_the_plan(workspace, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(workspace / "state"))
    touch(workspace, ".env")
    p = make_plan(workspace)
    assert p.result != "Ready" and any("inside the Workspace" in e for e in p.errors)


# ------------------------------------------------------------------ up (Seam 1, fake docker)


def up(ws, docker, capsys):
    code = main(["up", str(ws)], docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def secret_ws(workspace):
    touch(workspace, ".env", "TOKEN=abc")
    touch(workspace, ".ssh/id_rsa", "KEY")
    return workspace


def test_up_prepares_empty_readonly_mask_sources_before_starting(secret_ws, docker, capsys):
    docker.has_image()
    code, _, _ = up(secret_ws, docker, capsys)
    assert code == 0
    p = make_plan(secret_ws)
    f, d = p.mask_sources["file"], p.mask_sources["dir"]
    assert os.path.isfile(f) and os.path.getsize(f) == 0
    assert os.path.isdir(d) and os.listdir(d) == []
    assert stat.S_IMODE(os.stat(f).st_mode) & 0o222 == 0
    assert stat.S_IMODE(os.stat(d).st_mode) & 0o222 == 0


def test_up_replaces_a_tampered_mask_source_with_an_empty_one(secret_ws, docker, capsys):
    docker.has_image()
    p = make_plan(secret_ws)
    up(secret_ws, docker, capsys)
    main(["down", str(secret_ws)], docker=docker.command)
    f, d = p.mask_sources["file"], p.mask_sources["dir"]
    os.chmod(f, 0o644)
    with open(f, "w") as fh:
        fh.write("LEAKED")
    os.chmod(d, 0o755)
    (os.path.join(d, "x") and open(os.path.join(d, "x"), "w")).close()
    code, _, _ = up(secret_ws, docker, capsys)
    assert code == 0 and os.path.getsize(f) == 0 and os.listdir(d) == []


def test_up_replaces_a_symlinked_mask_source(secret_ws, docker, capsys, tmp_path):
    docker.has_image()
    p = make_plan(secret_ws)
    f = p.mask_sources["file"]
    os.makedirs(os.path.dirname(f))
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.symlink(victim, f)
    code, _, _ = up(secret_ws, docker, capsys)
    assert code == 0 and not os.path.islink(f) and victim.read_text() == "keep"


def test_up_starts_the_session_with_every_mask_mount(secret_ws, docker, capsys):
    docker.has_image()
    up(secret_ws, docker, capsys)
    (c,) = docker.containers.values()
    targets = {parse_mount(c["args"][i + 1])["target"] for i, a in enumerate(c["args"]) if a == "--mount"}
    assert {"/workspace", "/workspace/.env", "/workspace/.ssh"} <= targets


def test_up_fails_and_leaves_no_session_when_docker_cannot_apply_a_mount(secret_ws, docker, capsys):
    docker.has_image()
    docker.set(fail_run=True)
    code, _, err = up(secret_ws, docker, capsys)
    assert code != 0 and err
    assert docker.containers == {}


def test_up_fails_closed_when_the_started_session_lacks_a_mask(secret_ws, docker, capsys):
    docker.has_image()
    docker.set(drop_mounts=["/workspace/.env"])  # docker "succeeds" but the mask is missing
    code, _, err = up(secret_ws, docker, capsys)
    assert code != 0 and ".env" in err
    assert docker.containers == {}
    assert any(c[0] == "rm" and "-f" in c for c in docker.calls)


def test_up_fails_closed_when_a_mask_mount_is_writable(secret_ws, docker, capsys):
    docker.has_image()
    docker.set(writable_mounts=["/workspace/.ssh"])
    code, _, err = up(secret_ws, docker, capsys)
    assert code != 0 and ".ssh" in err and docker.containers == {}


def test_up_fails_closed_when_a_mask_mount_has_the_wrong_source(secret_ws, docker, capsys):
    docker.has_image()
    docker.set(wrong_source_mounts=["/workspace/.env"])
    code, _, err = up(secret_ws, docker, capsys)
    assert code != 0 and docker.containers == {}


def test_up_refuses_a_blocked_plan_before_touching_docker(workspace, docker, capsys):
    os.symlink(".", workspace / ".env")  # cannot be masked without hiding the Workspace
    code, _, err = up(workspace, docker, capsys)
    assert code != 0 and "Workspace root" in err
    assert docker.calls == []


def test_up_fails_when_mask_sources_cannot_be_prepared(secret_ws, docker, capsys, home):
    docker.has_image()
    state = home / ".local"
    state.write_text("a file where a directory is needed")
    code, _, err = up(secret_ws, docker, capsys)
    assert code != 0 and err
    assert not any(c[0] == "run" for c in docker.calls)
    assert docker.containers == {}


def test_up_twice_with_masks_is_still_a_noop(secret_ws, docker, capsys):
    docker.has_image()
    up(secret_ws, docker, capsys)
    runs = [c for c in docker.calls if c[0] == "run"]
    code, out, _ = up(secret_ws, docker, capsys)
    assert code == 0 and "already running" in out
    assert [c for c in docker.calls if c[0] == "run"] == runs


def test_originals_are_byte_identical_after_up_and_down(secret_ws, docker, capsys):
    docker.has_image()
    files = (secret_ws / ".env", secret_ws / ".ssh" / "id_rsa")

    def snap():
        return {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino, p.stat().st_mode) for p in files}

    before = snap()
    up(secret_ws, docker, capsys)
    main(["down", str(secret_ws)], docker=docker.command)
    assert snap() == before
    assert sorted(x.name for x in secret_ws.iterdir()) == [".env", ".ssh"]
