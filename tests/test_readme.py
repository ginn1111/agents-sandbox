"""The README must match the implementation (issue #13).

External behaviour only: the argparse parser, config validation, `asb check` output and the files in docs/.
"""
import argparse
import re
from pathlib import Path

import pytest
import yaml

from agent_sandbox import config as cfg_mod
from agent_sandbox.cli import _parser, main
from agent_sandbox.config import ConfigError, load_config
from agent_sandbox.plan import LIMITS

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text() if (ROOT / "README.md").exists() else ""


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def code_text(md: str) -> str:
    """Fenced blocks and inline code spans: where commands are written."""
    blocks = [re.sub(r"\s#.*", "", b) for b in re.findall(r"```[a-z]*\n(.*?)```", md, re.S)]  # comments are prose
    spans = re.findall(r"`([^`\n]+)`", re.sub(r"```.*?```", "", md, flags=re.S))
    return "\n".join(blocks + spans)


def subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = _parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def section(md: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    assert m, f"README has no '## {heading}' section"
    return m.group(1)


def test_readme_exists():
    assert README, "README.md is missing"


# ---------------------------------------------------------------- commands


def test_every_subcommand_is_documented():
    code = code_text(README)
    missing = [c for c in subcommands() if not re.search(rf"\basb {c}\b", code)]
    assert missing == []


def test_every_documented_subcommand_exists():
    code = code_text(README)
    known = set(subcommands())
    documented = set(re.findall(r"\basb ([a-z][a-z-]*)\b", code))
    assert documented - known == set()


def test_every_documented_flag_exists_in_the_parser():
    flags = set()
    for p in subcommands().values():
        for a in p._actions:
            flags.update(o for o in a.option_strings if o.startswith("--"))
    flags.discard("--help")
    flags.discard("--after-cli")  # internal, hidden from --help on purpose
    code = code_text(README)
    documented = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", code)) - {"--help"}
    # Flags that belong to host programs the README shows (Chrome, socat, ...), not to asb.
    external = {"--remote-debugging-port", "--user-data-dir", "--no-first-run", "--add-host"}
    assert documented - flags - external == set()
    assert flags <= documented, f"undocumented flags: {sorted(flags - documented)}"


def test_both_entry_points_are_documented():
    assert "agent-sandbox" in README and "`asb`" in README


def test_install_steps_cover_uv_pipx_linux_and_macos_colima():
    low = README.lower()
    assert "uv tool install" in low and "pipx install" in low
    assert "colima" in low and "macos" in low and "linux" in low
    assert "asb build" in code_text(README)


# ---------------------------------------------------------------- config


def _snippets():
    port = "port: 8080"
    return {
        "version": "version: 1",
        "resources.cpus": "resources: {cpus: 2}",
        "resources.memory": "resources: {memory: 2g}",
        "resources.pids_limit": "resources: {pids_limit: 100}",
        "secrets.mask": "secrets: {mask: ['**/x']}",
        "secrets.allow": "secrets: {allow: ['**/x']}",
        "secrets.skip": "secrets: {skip: ['build/']}",
        "network.host_services": "network: {host_services: [{name: a, host: host.docker.internal, port: 80}]}",
        "network.host_browser": "network: {host_browser: true}",
        "agents.<name>.command": "agents: {a: {command: [a]}}",
        "agents.<name>.env": "agents: {a: {command: [a], env: {X: y}}}",
        "portals.<name>.port": f"portals: {{p: {{{port}}}}}",
        "portals.<name>.command": f"portals: {{p: {{{port}, command: [x]}}}}",
        "portals.<name>.env": f"portals: {{p: {{{port}, env: {{X: y}}}}}}",
        "portals.<name>.open": f"portals: {{p: {{{port}, open: /ui}}}}",
        "portals.<name>.login": f"portals: {{p: {{{port}, command: [x], env: {{U: y, P: z}}, login: {{username: U, password: P}}}}}}",
    }


def readme_config_keys() -> set[str]:
    return set(re.findall(r"^\| `([a-z_.<>]+)` \|", section(README, "Config reference"), re.M))


def test_config_reference_lists_exactly_the_accepted_keys():
    assert readme_config_keys() == set(_snippets())


@pytest.mark.parametrize("key", sorted(_snippets()))
def test_each_documented_config_key_is_accepted(key, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    g = tmp_path / "g.yaml"
    body = _snippets()[key]
    g.write_text(body if key == "version" else f"version: 1\n{body}\n")
    load_config(ws, global_path=g)


def test_an_unknown_top_level_key_is_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    g = tmp_path / "g.yaml"
    g.write_text("version: 1\nnot_a_key: 1\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(ws, global_path=g)


def test_yaml_examples_in_the_readme_load(tmp_path):
    blocks = re.findall(r"```yaml\n(.*?)```", README, re.S)
    assert blocks, "README has no yaml examples"
    for i, body in enumerate(blocks):
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        assert yaml.safe_load(body).get("version") == 1, "every yaml example is a whole config file"
        g = tmp_path / f"g{i}.yaml"
        g.write_text(body)
        load_config(ws, global_path=g)


def test_project_config_exceptions_are_documented_and_true(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".agent-sandbox.yaml").write_text("version: 1\nnetwork: {host_browser: true}\n")
    with pytest.raises(ConfigError, match="host_browser"):
        load_config(ws, global_path=tmp_path / "none.yaml")
    (ws / ".agent-sandbox.yaml").write_text("version: 1\nportals: {p: {port: 80, secrets: {x: y}}}\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(ws, global_path=tmp_path / "none.yaml")
    low = norm(README).lower()
    assert "project config cannot" in low and "host browser" in low
    # `portals.<name>.secrets` was removed from the config schema; the README must not document it.
    assert "portals.<name>.secrets" not in README


def test_default_patterns_in_the_readme_are_the_ones_in_the_code():
    text = section(README, "Config reference")
    for pattern in cfg_mod.DEFAULT_MASK + cfg_mod.DEFAULT_ALLOW + cfg_mod.DEFAULT_SKIP:
        assert f"`{pattern}`" in text, pattern


def test_default_resources_agents_and_portals_in_the_readme_match_the_code():
    text = norm(section(README, "Config reference"))
    r = cfg_mod.DEFAULT_RESOURCES
    assert f"cpus: {r['cpus']}" in text and f"memory: {r['memory']}" in text and f"pids_limit: {r['pids_limit']}" in text
    for name, spec in cfg_mod.DEFAULT_PORTALS.items():
        assert f"`{name}`" in README and str(spec["port"]) in README
    for name in cfg_mod.DEFAULT_AGENTS:
        assert f"`{name}`" in text


# ---------------------------------------------------------------- known limits


# Each limit issue #13 requires, as a phrase that must appear in both `asb check` and the README.
REQUIRED_PHRASES = [
    "by filename only",
    "git history",
    "created after `up`",
    "Skip patterns",
    "later run on the host",
    "Host Services can return sensitive data",
    "Host Browser",
    "share the host kernel",
    "readable by the agent and by anything it runs",
    "OSC 52",
    "public internet",
    "hard link",
]


def check_output(workspace, docker, capsys) -> str:
    main(["check", str(workspace)], docker=docker.command)
    return capsys.readouterr().out


@pytest.mark.parametrize("phrase", REQUIRED_PHRASES)
def test_every_required_limit_phrase_is_in_check_and_in_the_readme(phrase, workspace, docker, capsys):
    out = norm(check_output(workspace, docker, capsys))
    assert phrase in out, f"`asb check` does not state: {phrase}"
    assert phrase in norm(section(README, "Known limits")), f"README known limits do not state: {phrase}"


def test_every_limit_line_check_prints_is_quoted_verbatim_in_the_readme(workspace, docker, capsys):
    out = check_output(workspace, docker, capsys)
    readme = norm(section(README, "Known limits"))
    printed = [norm(l.removeprefix("  - ")) for l in out.split("Limits:")[1].splitlines() if l.startswith("  - ")]
    assert printed == [norm(l) for l in LIMITS]
    assert [l for l in printed if l not in readme] == []


def test_readme_says_docker_behaviour_is_documented_from_design_and_unverified():
    text = norm(section(README, "Known limits"))
    assert "UNVERIFIED" in text
    assert "Seam 2" in text and "not yet run against a real Docker daemon" in text
    spikes = sorted((ROOT / "docs" / "spikes").glob("*.md"))
    assert spikes
    for spike in spikes:
        assert spike.name in text, f"known limits must list {spike.name}"


def test_readme_does_not_claim_protection_the_tool_does_not_give():
    low = norm(README).lower()
    for claim in ("fully secure", "completely safe", "guarantees", "prevents exfiltration", "100% secure",
                  "unbreakable", "tamper-proof", "cannot be bypassed"):
        assert claim not in low, claim
    assert "does not contain a hijacked" in low
