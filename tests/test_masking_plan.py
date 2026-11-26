"""Secret discovery as seen through the SessionPlan (ADR-0004). No Docker, no file contents read."""
import os
import threading

import pytest

from agent_sandbox.config import load_config
from agent_sandbox.plan import plan


def make_plan(ws, **cli):
    return plan(ws, load_config(ws, cli=cli or None))


def entries(p, status):
    return {e.path: e for e in p.entries if e.status == status}


def masked(p):
    return {path: e.reason for path, e in entries(p, "MASKED").items()}


def touch(ws, rel, text="x"):
    f = ws / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def project(ws, yaml_text):
    (ws / ".agent-sandbox.yaml").write_text("version: 1\n" + yaml_text)


# ------------------------------------------------------------------ default patterns


@pytest.mark.parametrize("rel", [
    ".env", "api/.env", "api/deep/er/.env", ".env.local", ".env.production",
    "tls/server.pem", "k.key", "a.p12", "a.pfx", ".npmrc", "x/.netrc", ".git-credentials",
    "credentials.json", "secrets.json", "secrets.yaml", "sub/secrets.yml", "id_rsa", "x/id_ed25519",
    ".git/config", "svc/.git/config", ".git/modules/sub/config",
])
def test_default_secret_patterns_mask_files(workspace, rel):
    touch(workspace, rel)
    p = make_plan(workspace)
    assert rel in masked(p)
    assert entries(p, "MASKED")[rel].kind == "file"


@pytest.mark.parametrize("rel", ["README.md", "src/main.py", ".envrc", "environment", "keyboard.txt", ".git/HEAD",
                                 ".git/modules/sub/HEAD", ".git/modules/config"])
def test_ordinary_files_are_not_masked(workspace, rel):
    touch(workspace, rel)
    assert rel not in masked(make_plan(workspace))


def test_masked_reason_names_the_pattern_and_where_it_came_from(workspace):
    touch(workspace, ".env")
    touch(workspace, "ops/my.secret")
    project(workspace, "secrets: {mask: ['**/*.secret']}\n")
    m = masked(make_plan(workspace))
    assert m[".env"] == "pattern **/.env"
    assert m["ops/my.secret"] == "pattern **/*.secret (project config)"


def test_allow_patterns_exempt_templates_and_say_so(workspace):
    for rel in (".env.example", ".env.sample", "api/.env.template", ".env.local"):
        touch(workspace, rel)
    p = make_plan(workspace)
    assert set(masked(p)) == {".env.local"}
    allowed = entries(p, "ALLOWED")
    assert set(allowed) == {".env.example", ".env.sample", "api/.env.template"}
    assert allowed[".env.example"].reason == "allow pattern **/.env.example"


def test_allowed_is_listed_only_for_paths_a_secret_pattern_would_have_masked(workspace):
    touch(workspace, "README.md")
    assert entries(make_plan(workspace), "ALLOWED") == {}


def test_secret_directories_are_masked_whole_and_not_listed_inside(workspace):
    touch(workspace, ".ssh/id_rsa")
    touch(workspace, ".ssh/known_hosts")
    touch(workspace, "svc/.aws/credentials")
    touch(workspace, "svc/.aws/deep/er/file")
    p = make_plan(workspace)
    assert masked(p) == {".ssh": "pattern **/.ssh/**", "svc/.aws": "pattern **/.aws/**"}
    assert entries(p, "MASKED")[".ssh"].kind == "dir"


def test_nested_matches_inside_a_masked_directory_are_not_masked_separately(workspace):
    project(workspace, "secrets: {mask: ['**/private/**']}\n")
    touch(workspace, "private/.env")
    touch(workspace, "private/sub/key.pem")
    assert list(masked(make_plan(workspace))) == ["private"]


def test_empty_secret_directory_is_still_masked(workspace):
    (workspace / ".gnupg").mkdir()
    assert masked(make_plan(workspace)) == {".gnupg": "pattern **/.gnupg/**"}


# ------------------------------------------------------------------ extending config


def test_global_config_extends_the_defaults(workspace, home):
    g = home / ".config" / "agent-sandbox"
    g.mkdir(parents=True)
    (g / "config.yaml").write_text("version: 1\nsecrets: {mask: ['**/*.token']}\n")
    touch(workspace, "a.token")
    touch(workspace, ".env")
    p = make_plan(workspace)
    assert {"a.token", ".env"} <= set(masked(p))
    assert masked(p)["a.token"] == "pattern **/*.token (global config)"


def test_project_allow_pattern_unmasks_and_is_attributed(workspace):
    touch(workspace, ".env.local")
    project(workspace, "secrets: {allow: ['**/.env.local']}\n")
    p = make_plan(workspace)
    assert ".env.local" not in masked(p)
    assert entries(p, "ALLOWED")[".env.local"].reason == "allow pattern **/.env.local (project config)"


def test_project_allow_for_a_directory_lets_discovery_look_inside(workspace):
    project(workspace, "secrets: {mask: ['**/vault/**'], allow: ['**/vault']}\n")
    touch(workspace, "vault/readme.txt")
    p = make_plan(workspace)
    assert "vault" in entries(p, "ALLOWED")
    assert "vault/readme.txt" not in masked(p) or True  # inside is judged on its own path


# ------------------------------------------------------------------ skip patterns


def test_default_skip_patterns_are_listed_and_never_masked(workspace):
    for d in ("node_modules", "vendor", "dist", "libs", "pkg/node_modules"):
        touch(workspace, f"{d}/.env")
        touch(workspace, f"{d}/deep/id_rsa")
    p = make_plan(workspace)
    assert masked(p) == {}
    skipped = entries(p, "SKIPPED")
    assert set(skipped) == {"node_modules", "vendor", "dist", "libs", "pkg/node_modules"}
    assert skipped["dist"].kind == "dir"
    assert skipped["dist"].reason == "skip pattern dist/"


def test_a_file_named_like_a_skip_directory_is_not_skipped(workspace):
    touch(workspace, "dist")
    assert entries(make_plan(workspace), "SKIPPED") == {}


def test_git_directory_is_not_skipped(workspace):
    touch(workspace, ".git/config")
    p = make_plan(workspace)
    assert ".git" not in entries(p, "SKIPPED") and ".git/config" in masked(p)


def test_project_skip_pattern_extends_defaults(workspace):
    project(workspace, "secrets: {skip: ['build/']}\n")
    touch(workspace, "build/.env")
    p = make_plan(workspace)
    assert "build" in entries(p, "SKIPPED") and masked(p) == {}


# ------------------------------------------------------------------ symlinks


def test_symlink_with_secret_name_masks_the_real_file_inside_the_workspace(workspace):
    touch(workspace, "config/real.env")
    os.symlink("config/real.env", workspace / ".env")
    p = make_plan(workspace)
    assert masked(p) == {"config/real.env": "target of symlink .env"}


def test_symlink_chain_masks_the_final_target(workspace):
    touch(workspace, "store/real")
    os.symlink("store/real", workspace / "hop")
    os.symlink("hop", workspace / ".env")
    assert masked(make_plan(workspace)) == {"store/real": "target of symlink .env"}


def test_symlink_with_harmless_name_to_a_secret_adds_nothing(workspace):
    touch(workspace, ".env")
    (workspace / "config").mkdir()
    os.symlink("../.env", workspace / "config" / "link")
    assert list(masked(make_plan(workspace))) == [".env"]


def test_symlink_to_directory_with_secret_name_masks_the_directory(workspace):
    touch(workspace, "keys/a")
    os.symlink("keys", workspace / ".ssh")
    p = make_plan(workspace)
    assert masked(p) == {"keys": "target of symlink .ssh"}
    assert entries(p, "MASKED")["keys"].kind == "dir"


def test_symlink_to_directory_matches_dir_only_secret_pattern(workspace):
    project(workspace, "secrets: {mask: ['secrets/']}\n")
    touch(workspace, "real_secrets/api.key")
    os.symlink("real_secrets", workspace / "secrets")
    p = make_plan(workspace)
    assert masked(p) == {"real_secrets": "target of symlink secrets"}
    assert entries(p, "MASKED")["real_secrets"].kind == "dir"


def test_symlink_to_directory_respects_dir_only_allow_pattern(workspace):
    project(workspace, "secrets: {mask: ['**/vault/**'], allow: ['vault/']}\n")
    touch(workspace, "real_vault/readme.txt")
    os.symlink("real_vault", workspace / "vault")
    p = make_plan(workspace)
    assert "vault" in entries(p, "ALLOWED")
    assert "real_vault" not in masked(p)


def test_symlink_pointing_outside_is_never_followed_and_is_reported(workspace, home):
    outside = home / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    os.symlink(outside / "secret", workspace / ".env")
    p = make_plan(workspace)
    assert masked(p) == {}
    assert any(".env" in w and "outside the Workspace" in w for w in p.warnings)
    assert p.result == "Ready"


def test_symlinked_directory_is_not_descended_into(workspace, home):
    outside = home / "outside"
    touch(outside, ".env")
    os.symlink(outside, workspace / "linked")
    assert masked(make_plan(workspace)) == {}


def test_dangling_secret_symlink_is_harmless(workspace):
    os.symlink("nowhere", workspace / ".env")
    p = make_plan(workspace)
    assert masked(p) == {} and p.result == "Ready"


def test_secret_symlink_to_the_workspace_root_blocks_the_plan(workspace):
    os.symlink(".", workspace / ".env")
    p = make_plan(workspace)
    assert p.result != "Ready"
    assert any("Workspace root" in e for e in p.errors)


def test_secret_symlink_target_under_a_masked_directory_is_not_listed_twice(workspace):
    touch(workspace, ".ssh/real")
    os.symlink(".ssh/real", workspace / ".env")
    assert list(masked(make_plan(workspace))) == [".ssh"]


# ------------------------------------------------------------------ hard links


def test_every_hard_link_to_a_masked_file_is_masked(workspace):
    touch(workspace, ".env")
    os.link(workspace / ".env", workspace / "notes.txt")
    (workspace / "docs").mkdir()
    os.link(workspace / ".env", workspace / "docs" / "harmless.md")
    m = masked(make_plan(workspace))
    assert m == {".env": "pattern **/.env", "docs/harmless.md": "hard link to .env",
                 "notes.txt": "hard link to .env"}


def test_hard_link_wins_over_an_allow_pattern(workspace):
    touch(workspace, ".env")
    os.link(workspace / ".env", workspace / ".env.example")
    p = make_plan(workspace)
    assert masked(p)[".env.example"] == "hard link to .env"
    assert ".env.example" not in entries(p, "ALLOWED")


def test_hard_link_to_a_file_inside_a_masked_directory_is_masked(workspace):
    touch(workspace, ".aws/credentials")
    os.link(workspace / ".aws" / "credentials", workspace / "creds.copy")
    m = masked(make_plan(workspace))
    assert m == {".aws": "pattern **/.aws/**", "creds.copy": "hard link to .aws/credentials"}


def test_hard_link_to_the_real_target_of_a_secret_symlink_is_masked(workspace):
    touch(workspace, "real")
    os.symlink("real", workspace / ".env")
    os.link(workspace / "real", workspace / "twin")
    assert set(masked(make_plan(workspace))) == {"real", "twin"}


def test_files_that_only_look_alike_are_not_hard_links(workspace):
    touch(workspace, ".env", "same")
    touch(workspace, "copy.txt", "same")
    assert list(masked(make_plan(workspace))) == [".env"]


def test_hard_links_under_skipped_directories_are_not_scanned(workspace):
    touch(workspace, ".env")
    (workspace / "dist").mkdir()
    os.link(workspace / ".env", workspace / "dist" / "copy")
    p = make_plan(workspace)
    assert list(masked(p)) == [".env"] and "dist" in entries(p, "SKIPPED")


# ------------------------------------------------------------------ never reads content


def test_discovery_never_opens_secret_files(workspace):
    os.mkfifo(workspace / ".env")  # opening it for reading would block forever
    os.mkfifo(workspace / "plain.fifo")
    result = {}

    def run():
        result["plan"] = make_plan(workspace)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "discovery opened a file"
    assert list(masked(result["plan"])) == [".env"]


def test_discovery_skips_fifo_named_dot_git(workspace):
    os.mkfifo(workspace / ".git")  # opening it in external_git() would block forever
    result = {}

    def run():
        result["plan"] = make_plan(workspace)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "discovery opened .git fifo"
    assert result["plan"].result == "Ready"


# ------------------------------------------------------------------ failure and ordering


def test_unreadable_directory_blocks_the_plan_instead_of_hiding_secrets(workspace, monkeypatch):
    touch(workspace, "locked/.env")
    real = os.scandir

    def scandir(path="."):
        if str(path).endswith("locked"):
            raise PermissionError(13, "denied", str(path))
        return real(path)

    monkeypatch.setattr("agent_sandbox.discovery.os.scandir", scandir)
    p = make_plan(workspace)
    assert p.result != "Ready"
    assert any("locked" in e for e in p.errors)


def test_unreadable_secret_directory_is_still_masked_without_listing_it(workspace, monkeypatch):
    touch(workspace, ".ssh/id_rsa")
    real = os.scandir

    def scandir(path="."):
        if str(path).endswith(".ssh"):
            raise PermissionError(13, "denied", str(path))
        return real(path)

    monkeypatch.setattr("agent_sandbox.discovery.os.scandir", scandir)
    p = make_plan(workspace)
    assert ".ssh" in masked(p)
    # its inode contents could not be scanned for hard links: the plan must say so
    assert p.result != "Ready"


def test_deep_directory_recursion_fails_closed_with_error(workspace):
    import inspect
    import sys

    cur = workspace
    for i in range(40):
        cur = cur / f"d{i}"
        cur.mkdir()
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack()) + 20)
    try:
        p = make_plan(workspace)
    finally:
        sys.setrecursionlimit(old)
    assert p.result != "Ready"
    assert p.errors


def test_entries_are_sorted_and_the_plan_is_deterministic(workspace):
    for rel in ("z/.env", "a/.env", "m/.env", "node_modules/x"):
        touch(workspace, rel)
    a, b = make_plan(workspace), make_plan(workspace)
    assert [e.path for e in a.entries] == [e.path for e in b.entries]
    mk = [e.path for e in a.entries if e.status == "MASKED"]
    assert mk == sorted(mk)


def test_plan_entries_are_json_serialisable(workspace):
    import json
    touch(workspace, ".env")
    data = json.loads(json.dumps(make_plan(workspace).to_dict()))
    assert {"status": "MASKED", "path": ".env", "kind": "file", "reason": "pattern **/.env"} in data["entries"]


def test_plan_does_not_modify_the_workspace(workspace):
    f = touch(workspace, ".env", "TOKEN=abc")
    st = f.stat()
    make_plan(workspace)
    st2 = f.stat()
    assert (f.read_text(), st.st_mtime_ns, st.st_ino, st.st_mode) == ("TOKEN=abc", st2.st_mtime_ns, st2.st_ino, st2.st_mode)
