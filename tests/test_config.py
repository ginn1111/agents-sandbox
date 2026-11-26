import textwrap

import pytest

from agent_sandbox.config import ConfigError, load_config


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    gpath = tmp_path / "global.yaml"

    def write(path, text):
        path.write_text(textwrap.dedent(text))

    return ws, gpath, write


def test_defaults_when_no_files_exist(env):
    ws, gpath, _ = env
    cfg = load_config(ws, global_path=gpath)
    assert cfg.resources == {"cpus": 4, "memory": "4g", "pids_limit": 512}
    assert cfg.loaded_files == []
    assert cfg.host_browser is False
    assert "**/.env" in [r.pattern for r in cfg.secret_mask]
    assert "**/.env.example" in [r.pattern for r in cfg.secret_allow]
    assert "node_modules/" in [r.pattern for r in cfg.secret_skip]


def test_precedence_cli_over_project_over_global_over_defaults(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nresources: {cpus: 8, memory: 8g, pids_limit: 100}\n")
    write(ws / ".agent-sandbox.yaml", "version: 1\nresources: {cpus: 2, memory: 2g}\n")
    cfg = load_config(ws, global_path=gpath, cli={"resources": {"cpus": 1}})
    assert cfg.resources == {"cpus": 1, "memory": "2g", "pids_limit": 100}
    assert cfg.resource_sources == {"cpus": "cli", "memory": "project", "pids_limit": "global"}


def test_loaded_files_are_reported(env):
    ws, gpath, write = env
    write(gpath, "version: 1\n")
    write(ws / ".agent-sandbox.yaml", "version: 1\n")
    cfg = load_config(ws, global_path=gpath)
    assert cfg.loaded_files == [str(gpath), str(ws / ".agent-sandbox.yaml")]


def test_unknown_top_level_key_is_rejected_with_file_and_key(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", "version: 1\nresorces: {cpus: 1}\n")
    with pytest.raises(ConfigError) as e:
        load_config(ws, global_path=gpath)
    assert "resorces" in str(e.value)
    assert ".agent-sandbox.yaml" in str(e.value)


def test_unknown_nested_key_is_rejected(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nresources: {cpu: 2}\n")
    with pytest.raises(ConfigError, match="resources.cpu"):
        load_config(ws, global_path=gpath)


@pytest.mark.parametrize(
    "body",
    [
        "resources: {cpus: lots}",
        "resources: {cpus: 0}",
        "resources: {pids_limit: -1}",
        "resources: {memory: 4 gigs}",
        "secrets: {mask: [1]}",
        "secrets: {mask: '**/x'}",
        "network: {host_services: [{name: a, host: h}]}",
        "network: {host_services: [{name: a, host: h, port: 70000}]}",
        "agents: {x: {command: hermes}}",
        "portals: {p: {command: [x]}}",
        "portals: {p: {port: 0}}",
        "version: 2",
    ],
)
def test_invalid_values_are_rejected(env, body):
    ws, gpath, write = env
    text = body if body.startswith("version") else f"version: 1\n{body}\n"
    write(gpath, text)
    with pytest.raises(ConfigError):
        load_config(ws, global_path=gpath)


def test_invalid_yaml_is_a_config_error(env):
    ws, gpath, write = env
    write(gpath, "version: [1\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(ws, global_path=gpath)


def test_missing_version_is_rejected(env):
    ws, gpath, write = env
    write(gpath, "resources: {cpus: 1}\n")
    with pytest.raises(ConfigError, match="version"):
        load_config(ws, global_path=gpath)


def test_secret_lists_add_to_defaults_and_keep_their_source(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nsecrets: {mask: ['**/g.txt']}\n")
    write(ws / ".agent-sandbox.yaml", "version: 1\nsecrets: {mask: ['**/p.txt'], allow: ['**/.env.local']}\n")
    cfg = load_config(ws, global_path=gpath)
    by_pattern = {r.pattern: r.source for r in cfg.secret_mask}
    assert by_pattern["**/.env"] == "default"
    assert by_pattern["**/g.txt"] == "global"
    assert by_pattern["**/p.txt"] == "project"
    assert {r.pattern: r.source for r in cfg.secret_allow}["**/.env.local"] == "project"


def test_project_rules_are_listed_for_check(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", "version: 1\nsecrets: {allow: ['**/.env.local']}\nresources: {cpus: 2}\n")
    cfg = load_config(ws, global_path=gpath)
    assert ("secrets.allow", "**/.env.local") in cfg.project_rules
    assert ("resources.cpus", "2") in cfg.project_rules


def test_project_config_cannot_enable_host_browser(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", "version: 1\nnetwork: {host_browser: true}\n")
    with pytest.raises(ConfigError, match="host_browser"):
        load_config(ws, global_path=gpath)


def test_global_config_can_set_host_browser(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nnetwork: {host_browser: true}\n")
    assert load_config(ws, global_path=gpath).host_browser is True


def test_project_config_cannot_set_portal_secrets(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", "version: 1\nportals: {p: {port: 80, env: {TOKEN: abc}}}\n")
    load_config(ws, global_path=gpath)  # a literal env value is fine
    write(ws / ".agent-sandbox.yaml", "version: 1\nportals: {p: {port: 80, secrets: {x: y}}}\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(ws, global_path=gpath)
    write(gpath, "version: 1\nportals: {p: {port: 80, secrets: {x: y}}}\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(ws, global_path=gpath)


@pytest.mark.parametrize("bad", [
    "resources: {pids_limit: 1.5}",
    "resources: {pids_limit: true}",
    "resources: {cpus: .nan}",
    "resources: {cpus: .inf}",
])
def test_resources_rejects_invalid_pids_limit_and_non_finite_cpus(env, bad):
    ws, gpath, write = env
    write(gpath, f"version: 1\n{bad}\n")
    with pytest.raises(ConfigError):
        load_config(ws, global_path=gpath)


def test_cli_offline_flag(env):
    ws, gpath, _ = env
    assert load_config(ws, global_path=gpath, cli={"offline": True}).offline is True
    assert load_config(ws, global_path=gpath).offline is False


def test_cli_cannot_smuggle_unknown_keys(env):
    ws, gpath, _ = env
    with pytest.raises(ConfigError, match="bogus"):
        load_config(ws, global_path=gpath, cli={"bogus": 1})


# ---------------------------------------------------------------- agents (ticket 06)


def test_default_agents_have_no_env(env):
    ws, gpath, _ = env
    cfg = load_config(ws, global_path=gpath)
    assert set(cfg.agents) == {"hermes", "omp", "claude", "codex"}
    assert cfg.agent_env == {}


def test_agent_env_is_loaded_and_merged_per_agent(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nagents:\n  hermes: {command: [hermes], env: {A: '1'}}\n")
    write(ws / ".agent-sandbox.yaml", "version: 1\nagents:\n  hermes: {command: [hermes, chat], env: {B: '2'}}\n")
    cfg = load_config(ws, global_path=gpath)
    assert cfg.agents["hermes"] == ["hermes", "chat"]
    assert cfg.agent_env["hermes"] == {"B": "2"}  # project entry replaces the global entry
    assert ("agents.hermes.env", "B") in cfg.project_rules  # key names only, never values


@pytest.mark.parametrize(
    "body",
    [
        "agents: {'../x': {command: [x]}}",
        "agents: {'Up': {command: [x]}}",
        "agents: {'a b': {command: [x]}}",
        "agents: {'': {command: [x]}}",
        "agents: {x: {command: [x], env: [A]}}",
        "agents: {x: {command: [x], env: {A: 1}}}",
        "agents: {x: {command: [x], env: {'1A': a}}}",
        "agents: {x: {command: [x], env: {'A=B': a}}}",
    ],
)
def test_invalid_agent_names_and_env_are_rejected(env, body):
    ws, gpath, write = env
    write(gpath, f"version: 1\n{body}\n")
    with pytest.raises(ConfigError):
        load_config(ws, global_path=gpath)


# --- Portals (ADR-0010, ADR-0013)


def test_plannotator_is_a_builtin_portal(env):
    ws, gpath, _ = env
    p = load_config(ws, global_path=gpath).portals["plannotator"]
    assert p["port"] == 19432
    assert p["env"]["PLANNOTATOR_REMOTE"] == "1"
    assert p["open"] == "/" and "command" not in p


def test_a_portal_entry_with_the_same_name_replaces_the_builtin(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nportals: {plannotator: {port: 20000}}\n")
    assert load_config(ws, global_path=gpath).portals["plannotator"] == {"port": 20000}


def test_portals_take_port_command_env_and_open(env):
    ws, gpath, write = env
    write(gpath, 'version: 1\nportals:\n  docs: {port: 8080, command: [serve, "8080"], env: {A: "1"}, open: /index.html}\n')
    assert load_config(ws, global_path=gpath).portals["docs"] == {
        "port": 8080, "command": ["serve", "8080"], "env": {"A": "1"}, "open": "/index.html"}


@pytest.mark.parametrize(
    "body",
    [
        "portals: {p: {port: 80, bind: 0.0.0.0}}",          # unknown key
        "portals: {p: {port: 80, open: index.html}}",        # open is a URL path
        "portals: {p: {port: 80, open: 3}}",
        "portals: {p: {port: 80, env: {1BAD: x}}}",
        "portals: {p: {port: 80, env: {A: 1}}}",             # values are strings
        "portals: {p: {port: 80, env: [A]}}",
        "portals: {'Bad Name': {port: 80}}",                 # names become container names
        "portals: {'../x': {port: 80}}",
        "portals: {p: {port: '80'}}",
    ],
)
def test_invalid_portal_entries_are_rejected(env, body):
    ws, gpath, write = env
    write(gpath, f"version: 1\n{body}\n")
    with pytest.raises(ConfigError):
        load_config(ws, global_path=gpath)


def test_project_portals_are_listed_as_project_rules_including_their_command(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", "version: 1\nportals: {docs: {port: 8080, command: [serve, x]}}\n")
    rules = dict(load_config(ws, global_path=gpath).project_rules)
    assert rules["portals.docs"] == "port 8080"
    assert rules["portals.docs.command"] == "serve x"
