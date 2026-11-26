"""Seam 2: end-to-end against a real Docker daemon. Skipped when `docker` is unavailable.

Probes what an agent inside the Session can actually see. Run with `uv run pytest -m docker`.
"""
import shutil
import subprocess

import pytest

from agent_sandbox.cli import IMAGE_CONTEXT, main
from agent_sandbox.plan import IMAGE, session_name

pytestmark = pytest.mark.docker


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


if not _docker_ok():
    pytest.skip("no reachable Docker daemon", allow_module_level=True)


def sh(name, script):
    return subprocess.run(["docker", "exec", name, "sh", "-c", script], capture_output=True, text=True)


@pytest.fixture
def session(workspace):
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_workspace_edits_persist_and_home_is_invisible(session, workspace, home):
    (workspace / "a.txt").write_text("host")
    assert sh(session, "cat /workspace/a.txt").stdout == "host"
    sh(session, "echo agent > /workspace/b.txt")
    assert (workspace / "b.txt").read_text().strip() == "agent"
    assert sh(session, f"ls {home}").returncode != 0
    assert sh(session, "ls /workspace/..").returncode == 0  # container root, not host parent
    assert "user" not in sh(session, "ls /home").stdout
    assert sh(session, "test -e /var/run/docker.sock").returncode != 0
    assert sh(session, "id -u").stdout.strip() != "0"
    assert sh(session, "touch /usr/x").returncode != 0  # read-only root


def test_two_sessions_do_not_collide(home):
    a, b = home / "x" / "api", home / "y" / "api"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    try:
        assert main(["build"]) == 0
        assert main(["up", str(a)]) == 0 and main(["up", str(b)]) == 0
    finally:
        main(["down", str(a)])
        main(["down", str(b)])


# --------------------------------------------------------------------------- Secret masking (ticket #3)
#
# UNVERIFIED on the machine this was written on (no Docker daemon). They encode the acceptance criteria
# and must be run with `uv run pytest -m docker` on a host with Docker before the ticket is closed.

import os  # noqa: E402


@pytest.fixture
def secret_session(workspace):
    (workspace / ".env").write_text("TOKEN=abc\n")
    (workspace / "app").mkdir()
    (workspace / "app" / ".env.local").write_text("LOCAL=1\n")
    (workspace / ".env.example").write_text("TOKEN=\n")
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "id_rsa").write_text("PRIVATE\n")
    os.link(workspace / ".env", workspace / "notes.txt")
    (workspace / "config").mkdir()
    (workspace / "config" / "real.secretfile").write_text("REAL\n")
    os.symlink("config/real.secretfile", workspace / "secrets.json")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / ".env").write_text("SKIPPED=1\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    yield name
    main(["down", str(workspace)])


def test_masked_files_read_as_empty_and_templates_stay_readable(secret_session, workspace):
    for rel in (".env", "app/.env.local", "notes.txt", "config/real.secretfile"):
        r = sh(secret_session, f"wc -c < /workspace/{rel}")
        assert r.returncode == 0 and r.stdout.strip() == "0", rel
    assert sh(secret_session, "cat /workspace/.env.example").stdout == "TOKEN=\n"
    assert sh(secret_session, "cat /workspace/node_modules/.env").stdout == "SKIPPED=1\n"  # Skip pattern: not masked


def test_masked_files_cannot_be_written_deleted_or_renamed(secret_session, workspace):
    for rel in (".env", "notes.txt", "config/real.secretfile"):
        assert sh(secret_session, f"echo pwned > /workspace/{rel}").returncode != 0
        assert sh(secret_session, f"rm -f /workspace/{rel}").returncode != 0
        assert sh(secret_session, f"mv /workspace/{rel} /workspace/moved").returncode != 0
        assert sh(secret_session, f"truncate -s 0 /workspace/{rel}").returncode != 0
    assert sh(secret_session, "test -e /workspace/moved").returncode != 0


def test_masked_directories_appear_empty_and_read_only(secret_session):
    assert sh(secret_session, "ls -A /workspace/.ssh").stdout == ""
    assert sh(secret_session, "touch /workspace/.ssh/new").returncode != 0
    assert sh(secret_session, "rmdir /workspace/.ssh").returncode != 0
    assert sh(secret_session, "mv /workspace/.ssh /workspace/ssh2").returncode != 0


def test_host_originals_are_byte_identical_after_up_and_down(workspace):
    files = {".env": b"TOKEN=abc\n", "id_rsa": b"PRIVATE\n"}
    for name, data in files.items():
        (workspace / name).write_bytes(data)
    before = {n: (workspace / n).stat() for n in files}
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    sh(session_name(workspace.resolve()), "echo x > /workspace/.env; rm -f /workspace/id_rsa")  # must fail
    assert main(["down", str(workspace)]) == 0
    for n, data in files.items():
        st = (workspace / n).stat()
        assert (workspace / n).read_bytes() == data
        assert (st.st_mtime_ns, st.st_ino, st.st_mode, st.st_nlink) == (
            before[n].st_mtime_ns, before[n].st_ino, before[n].st_mode, before[n].st_nlink)


def test_up_leaves_no_session_when_a_mask_cannot_be_applied(workspace, monkeypatch):
    """Force a mount failure: a Secret file that vanishes between planning and `up` must abort `up`."""
    from agent_sandbox import masks

    (workspace / ".env").write_text("TOKEN=abc\n")
    assert main(["build"]) == 0
    real = masks.check_originals_exist

    def vanish(plan):
        os.unlink(workspace / ".env")
        return real(plan)

    monkeypatch.setattr("agent_sandbox.runtime.check_originals_exist", vanish)
    assert main(["up", str(workspace)]) != 0
    ps = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={session_name(workspace.resolve())}"],
                        capture_output=True, text=True)
    assert ps.stdout.strip() == ""
    assert not (workspace / ".env").exists()  # docker must not have created a mount point inside the Workspace


GIT = "git -c user.name=asb -c user.email=asb@example.invalid"


def test_git_config_is_masked_and_local_git_still_works(workspace):
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://user:TOKEN@example.invalid/r.git"],
                   check=True)
    (workspace / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(workspace), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=h", "-c", "user.email=h@example.invalid",
                    "commit", "-qm", "first"], check=True)
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    name = session_name(workspace.resolve())
    try:
        assert sh(name, "wc -c < /workspace/.git/config").stdout.strip() == "0"
        assert "TOKEN" not in sh(name, "git -C /workspace remote -v; git -C /workspace config --list").stdout
        script = (
            "cd /workspace && git status --short && echo two >> a.txt && git diff --stat | grep a.txt"
            f" && git branch topic && {GIT} checkout -q topic && git add a.txt"
            f" && {GIT} commit -qm second && git log --oneline | wc -l | grep -qx 2"
        )
        r = sh(name, script)
        assert r.returncode == 0, r.stdout + r.stderr
        assert sh(name, "cd /workspace && git remote -v").stdout.strip() == ""  # no remotes inside the Session
    finally:
        main(["down", str(workspace)])
    log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"], capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 2  # the Session's local commit is on the host repo
    cfg = subprocess.run(["git", "-C", str(workspace), "remote", "-v"], capture_output=True, text=True).stdout
    assert "TOKEN" in cfg  # host config untouched


def test_submodule_config_is_masked(workspace):
    cfg = workspace / ".git" / "modules" / "sub" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[remote \"origin\"]\n\turl = https://u:TOKEN@example.invalid/s.git\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    try:
        assert sh(session_name(workspace.resolve()), "wc -c < /workspace/.git/modules/sub/config").stdout.strip() == "0"
    finally:
        main(["down", str(workspace)])
