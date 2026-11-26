"""`asb check` output for Secret masking (ADR-0004, ADR-0005, SPEC 5)."""
import json
import os

from agent_sandbox.cli import main


def run(argv, docker, capsys):
    code = main(argv, docker=docker.command)
    out = capsys.readouterr()
    return code, out.out, out.err


def touch(ws, rel, text="x"):
    f = ws / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def lines(out, status):
    return [l for l in out.splitlines() if l.split(" ")[0] == status]


def test_check_lists_masked_allowed_and_skipped_with_reasons(workspace, docker, capsys):
    touch(workspace, ".env")
    touch(workspace, ".env.example")
    touch(workspace, ".git/config")
    touch(workspace, "node_modules/x/.env")
    os.link(workspace / ".env", workspace / "notes.txt")
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0
    masked = "\n".join(lines(out, "MASKED"))
    assert ".env" in masked and "pattern **/.env" in masked
    assert "notes.txt" in masked and "hard link to .env" in masked
    assert ".git/config" in masked
    assert any(".env.example" in l and "allow pattern" in l for l in lines(out, "ALLOWED"))
    assert any("node_modules/" in l and "skip pattern" in l for l in lines(out, "SKIPPED"))
    assert docker.calls == []


def test_check_lists_symlink_targets(workspace, docker, capsys):
    touch(workspace, "config/real.env")
    os.symlink("config/real.env", workspace / ".env")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert any("config/real.env" in l and "target of symlink .env" in l for l in lines(out, "MASKED"))


def test_check_marks_masked_directories_with_a_trailing_slash(workspace, docker, capsys):
    touch(workspace, ".ssh/id_rsa")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert any(l.split()[1] == ".ssh/" for l in lines(out, "MASKED"))


def test_check_states_the_limits_about_masking(workspace, docker, capsys):
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    low = out.lower()
    assert "filename only" in low
    assert "git history" in low
    assert "after `up`" in low and "skip patterns" in low
    assert "hard link" in low


def test_check_flags_project_rules_that_weaken_masking(workspace, docker, capsys):
    (workspace / ".agent-sandbox.yaml").write_text(
        "version: 1\nsecrets: {allow: ['**/.env.local'], mask: ['**/*.tok'], skip: ['build/']}\n")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert "Project config: secrets.allow **/.env.local (weakens masking)" in out
    assert "Project config: secrets.skip build/ (weakens masking)" in out
    assert "Project config: secrets.mask **/*.tok" in out and "secrets.mask **/*.tok (weakens" not in out


def test_check_without_secrets_says_so_and_does_not_claim_protection(workspace, docker, capsys):
    touch(workspace, "README.md")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert lines(out, "MASKED") == []
    assert "No Secret files found" in out
    assert "not masked yet" not in out


def test_check_json_carries_entries_and_mask_mounts(workspace, docker, capsys):
    touch(workspace, ".env")
    _, out, _ = run(["check", "--json", str(workspace)], docker, capsys)
    data = json.loads(out)
    assert {"status": "MASKED", "path": ".env", "kind": "file", "reason": "pattern **/.env"} in data["entries"]
    assert "target=/workspace/.env" in " ".join(data["session_run"])
    assert data["errors"] == []


def test_check_reports_symlinks_pointing_outside_as_warnings(workspace, home, docker, capsys):
    touch(home / "o", "s")
    os.symlink(home / "o" / "s", workspace / ".env")
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0 and "outside the Workspace" in out


def test_check_blocked_plan_shows_the_error_and_exits_nonzero(workspace, docker, capsys):
    os.symlink(".", workspace / ".env")
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code != 0
    assert "Workspace root" in out and "Result: Blocked" in out


def test_check_warns_about_external_git_metadata(workspace, docker, capsys):
    (workspace / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0 and "outside the Workspace" in out and "not mounted" in out


def test_check_handles_os_error_reading_git_pointer(workspace, docker, capsys, monkeypatch):
    (workspace / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/x\n")
    real_open = os.open

    def denied(path, *args, **kwargs):
        if os.fspath(path) in (".git", str(workspace / ".git")):
            raise PermissionError(13, "denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    code, out, _ = run(["check", str(workspace)], docker, capsys)
    assert code == 0 and "cannot inspect git pointer .git" in out


def test_external_git_metadata_in_a_nested_directory_is_warned(workspace, docker, capsys):
    (workspace / "sub").mkdir()
    (workspace / "sub" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/sub\n")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert "sub/.git points to" in out


def test_git_file_pointing_inside_the_workspace_is_not_warned(workspace, docker, capsys):
    touch(workspace, ".git/modules/sub/HEAD")
    (workspace / "sub").mkdir()
    (workspace / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
    _, out, _ = run(["check", str(workspace)], docker, capsys)
    assert "sub/.git points to" not in out
