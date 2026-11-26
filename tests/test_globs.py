"""Glob matching for Secret, allow and Skip patterns (relative POSIX paths from the Workspace)."""
import pytest

from agent_sandbox.globs import compile_glob


def m(pattern, path, is_dir=False):
    return compile_glob(pattern).matches(path, is_dir)


@pytest.mark.parametrize("path", [".env", "a/.env", "a/b/c/.env"])
def test_double_star_prefix_matches_at_any_depth_including_the_root(path):
    assert m("**/.env", path)


def test_exact_name_does_not_match_longer_or_partial_names():
    assert not m("**/.env", ".env.local")
    assert not m("**/.env", "a/my.env")
    assert not m("**/.env", "a/.env/x")


def test_env_star_matches_suffixed_names_but_not_plain_env():
    assert m("**/.env.*", "api/.env.production")
    assert not m("**/.env.*", ".env")


def test_star_does_not_cross_directories():
    assert m("config/*.key", "config/a.key")
    assert not m("config/*.key", "config/sub/a.key")


def test_question_mark_and_character_classes():
    assert m("**/id_?sa", "id_rsa") and not m("**/id_?sa", "id_rsaa")
    assert m("**/k[0-9].pem", "k7.pem") and not m("**/k[0-9].pem", "ka.pem")
    assert m("**/k[!0-9].pem", "ka.pem")


def test_trailing_double_star_matches_everything_inside_and_the_directory_itself():
    assert m("**/.ssh/**", ".ssh", is_dir=True)
    assert m("**/.ssh/**", ".ssh/id_rsa")
    assert m("**/.ssh/**", "home/.ssh/keys/deep/x")
    assert not m("**/.ssh/**", "home/.sshx/a")
    assert not m("**/.ssh/**", "a.ssh/x")


def test_middle_double_star_matches_zero_or_more_directories():
    assert m("**/.git/modules/*/config", ".git/modules/sub/config")
    assert m("**/.git/modules/*/config", "x/.git/modules/sub/config")
    assert not m("**/.git/modules/*/config", ".git/modules/config")
    assert m("a/**/z", "a/z") and m("a/**/z", "a/b/c/z")


def test_pattern_without_slash_matches_that_name_at_any_depth():
    assert m("secrets.txt", "secrets.txt") and m("secrets.txt", "a/b/secrets.txt")
    assert m("*.pem", "deep/x.pem")


def test_leading_slash_anchors_to_the_workspace_root():
    assert m("/.env", ".env") and not m("/.env", "a/.env")


def test_pattern_with_inner_slash_is_anchored_to_the_root():
    assert m("config/app.key", "config/app.key")
    assert not m("config/app.key", "x/config/app.key")


def test_trailing_slash_matches_directories_only():
    assert m("node_modules/", "node_modules", is_dir=True)
    assert m("node_modules/", "a/b/node_modules", is_dir=True)
    assert not m("node_modules/", "node_modules", is_dir=False)
    assert not m("node_modules/", "a/node_modules_x", is_dir=True)


def test_regex_metacharacters_in_patterns_are_literal():
    assert m("**/a+b(1).txt", "a+b(1).txt")
    assert not m("**/a.b", "axb")


def test_matching_is_case_sensitive():
    assert not m("**/.env", ".ENV")
