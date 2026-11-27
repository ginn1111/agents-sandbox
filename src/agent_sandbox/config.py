"""Configuration loading, merging and strict validation (ADR-0005, SPEC section 9).

Precedence: CLI > project ``.agent-sandbox.yaml`` > global config > defaults.
Unknown or invalid keys are rejected, never ignored.
"""
from __future__ import annotations

import copy
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import host_browser as _hb
from . import host_services as _hs

PROJECT_FILE = ".agent-sandbox.yaml"
SOURCES = ("default", "global", "project", "cli")

DEFAULT_MASK = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
    "**/.npmrc", "**/.netrc", "**/.git-credentials", "**/.ssh/**", "**/.aws/**",
    "**/.gnupg/**", "**/credentials.json", "**/secrets.json", "**/secrets.yaml",
    "**/secrets.yml", "**/id_rsa", "**/id_ed25519", "**/.git/config",
    "**/.git/modules/*/config",
]
DEFAULT_ALLOW = ["**/.env.example", "**/.env.sample", "**/.env.template"]
DEFAULT_SKIP = ["node_modules/", "vendor/", "dist/", "libs/"]
DEFAULT_RESOURCES = {"cpus": 4, "memory": "4g", "pids_limit": 512}
DEFAULT_AGENTS = {
    "hermes": ["hermes"], "omp": ["omp"], "claude": ["claude"], "codex": ["codex"],
}

_MEMORY = re.compile(r"^[1-9][0-9]*[bkmg]?$", re.IGNORECASE)
# Portal names become container-side identifiers and are typed on the command line.
PORTAL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
# Built-in Portal entries (ADR-0010). A config entry with the same name replaces one as a whole.
DEFAULT_PORTALS = {
    "plannotator": {
        "port": 19432,
        "env": {"PLANNOTATOR_REMOTE": "1", "PLANNOTATOR_PORT": "19432"},
        "open": "/",
    },
    # ADR-0010, ADR-0020. The dashboard listens on 0.0.0.0, so Hermes requires a login: the CLI
    # generates it once per Workspace. HERMES_WEB_DIST: the UI is built into the image, because
    # the read-only root filesystem leaves Hermes nowhere to build it at start.
    "hermes-dashboard": {
        "port": 9119,
        "command": ["hermes", "dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"],
        "env": {
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "{generated}",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "{generated}",
            "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "{generated}",
            "HERMES_WEB_DIST": "/opt/hermes/src/hermes_cli/web_dist",
        },
        "login": {
            "username": "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
            "password": "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        },
        "open": "/",
    },
}
# Agent names become Docker volume names and directory names, so keep them plain.
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Set by the Session itself; an agent entry may not override them.
RESERVED_ENV = {"HOME"}


class ConfigError(ValueError):
    """A config file or CLI option is invalid."""


@dataclass(frozen=True)
class Rule:
    pattern: str
    source: str  # one of SOURCES


@dataclass
class Config:
    resources: dict[str, Any]
    resource_sources: dict[str, str]
    secret_mask: list[Rule]
    secret_allow: list[Rule]
    secret_skip: list[Rule]
    host_services: list[dict[str, Any]]
    host_browser: bool
    agents: dict[str, list[str]]
    portals: dict[str, dict[str, Any]]
    offline: bool
    loaded_files: list[str] = field(default_factory=list)
    # agent name -> env for that agent only (ADR-0007). Absent when the agent has none.
    agent_env: dict[str, dict[str, str]] = field(default_factory=dict)
    # (config key, value) for every setting that came from project config.
    project_rules: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- validation


def _where(label: str, key: str) -> str:
    return f"{label}: {key}"


def _expect_map(value: Any, label: str, key: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{_where(label, key)} must be a mapping")
    return value


def _reject_unknown(mapping: dict, allowed: set[str], label: str, prefix: str = "") -> None:
    for k in mapping:
        if k not in allowed:
            full = f"{prefix}{k}"
            raise ConfigError(f"{label}: unknown key '{full}' (allowed: {', '.join(sorted(allowed))})")


def _validate_resources(res: Any, label: str) -> dict[str, Any]:
    res = _expect_map(res, label, "resources")
    _reject_unknown(res, set(DEFAULT_RESOURCES), label, "resources.")
    out: dict[str, Any] = {}
    for k, v in res.items():
        if k == "memory":
            if not isinstance(v, (str, int)) or isinstance(v, bool) or not _MEMORY.match(str(v)):
                raise ConfigError(f"{label}: resources.memory must look like 4g, 512m or a byte count")
            out[k] = str(v)
        elif k == "pids_limit":
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ConfigError(f"{label}: resources.pids_limit must be a positive integer")
            out[k] = v
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ConfigError(f"{label}: resources.{k} must be a positive finite number")
            out[k] = v
    return out


def _validate_str_list(value: Any, label: str, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{label}: {key} must be a list of non-empty strings")
    return list(value)


def _validate_port(v: Any, label: str, key: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
        raise ConfigError(f"{label}: {key} must be a port between 1 and 65535")
    return v


def _validate_secrets(sec: Any, label: str) -> dict[str, list[str]]:
    sec = _expect_map(sec, label, "secrets")
    _reject_unknown(sec, {"mask", "allow", "skip"}, label, "secrets.")
    return {k: _validate_str_list(v, label, f"secrets.{k}") for k, v in sec.items()}


def _validate_network(net: Any, label: str, source: str) -> dict[str, Any]:
    net = _expect_map(net, label, "network")
    _reject_unknown(net, {"host_services", "host_browser"}, label, "network.")
    out: dict[str, Any] = {}
    if "host_browser" in net:
        if source == "project":
            raise ConfigError(
                f"{label}: network.host_browser cannot be set in project config; "
                "enable it from global config or the CLI (--host-browser) (ADR-0005, ADR-0008)"
            )
        if not isinstance(net["host_browser"], bool):
            raise ConfigError(f"{label}: network.host_browser must be true or false")
        out["host_browser"] = net["host_browser"]
    if "host_services" in net:
        items = net["host_services"]
        if not isinstance(items, list):
            raise ConfigError(f"{label}: network.host_services must be a list")
        services = []
        for i, item in enumerate(items):
            item = _expect_map(item, label, f"network.host_services[{i}]")
            _reject_unknown(item, {"name", "host", "port"}, label, f"network.host_services[{i}].")
            for req in ("name", "host", "port"):
                if req not in item:
                    raise ConfigError(f"{label}: network.host_services[{i}] is missing '{req}'")
            if not isinstance(item["name"], str) or not isinstance(item["host"], str):
                raise ConfigError(f"{label}: network.host_services[{i}] name and host must be strings")
            for key, problem in (("name", _hs.name_error(item["name"])), ("host", _hs.host_error(item["host"]))):
                if problem:
                    raise ConfigError(f"{label}: network.host_services[{i}].{key} {problem}")
            if any(s["name"] == item["name"] for s in services):
                raise ConfigError(f"{label}: network.host_services name '{item['name']}' is listed more than once")
            _validate_port(item["port"], label, f"network.host_services[{i}].port")
            reserved = _hb.reserved_error(item["name"], item["host"], item["port"])
            if reserved:
                raise ConfigError(f"{label}: network.host_services[{i}] {reserved}")
            services.append(dict(item))
        out["host_services"] = services
    return out


def _validate_agent_env(env: Any, label: str, key: str) -> dict[str, str]:
    env = _expect_map(env, label, key)
    out = {}
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_NAME.fullmatch(k):
            raise ConfigError(f"{label}: {key} has an invalid variable name {k!r}")
        if k in RESERVED_ENV:
            raise ConfigError(f"{label}: {key}.{k} is set by the Session and cannot be overridden")
        if not isinstance(v, str) or "\0" in v:
            raise ConfigError(f"{label}: {key}.{k} must be a string without NUL bytes")
        out[k] = v
    return out


def _validate_agents(agents: Any, label: str) -> dict[str, dict[str, Any]]:
    agents = _expect_map(agents, label, "agents")
    out = {}
    for name, spec in agents.items():
        if not isinstance(name, str) or not AGENT_NAME.fullmatch(name):
            raise ConfigError(
                f"{label}: agents.{name}: agent names must match {AGENT_NAME.pattern} "
                "(they name the Agent volume)"
            )
        spec = _expect_map(spec, label, f"agents.{name}")
        _reject_unknown(spec, {"command", "env"}, label, f"agents.{name}.")
        if "command" not in spec:
            raise ConfigError(f"{label}: agents.{name} is missing 'command'")
        entry: dict[str, Any] = {"command": _validate_str_list(spec["command"], label, f"agents.{name}.command")}
        if not entry["command"] or any("\0" in arg for arg in entry["command"]):
            raise ConfigError(f"{label}: agents.{name}.command must contain a program and no NUL bytes")
        if "env" in spec:
            entry["env"] = _validate_agent_env(spec["env"], label, f"agents.{name}.env")
        out[name] = entry
    return out


def _validate_login(login: Any, spec: dict, label: str, name: str, source: str) -> None:
    """`login` names the env variables `portal start` shows as the user name and password."""
    key = f"portals.{name}.login"
    login = _expect_map(login, label, key)
    _reject_unknown(login, {"username", "password"}, label, f"{key}.")
    env = spec.get("env", {})
    for k in ("username", "password"):
        var = login.get(k)
        if not isinstance(var, str) or var not in env:
            raise ConfigError(f"{label}: {key}.{k} must name a variable in portals.{name}.env")
        if source == "project" and env[var] != "{generated}":
            raise ConfigError(
                f"{label}: {key}: {var} must be {{generated}} in project config; "
                "Portal logins are not set from the project (ADR-0005, ADR-0010)")


def _keep_generated(previous: dict | None, spec: dict, name: str) -> None:
    """Project config may replace a Portal entry, but not a `{generated}` secret of the entry it
    replaces (defaults or global config) with a literal: Portal secrets are never set from the
    project (ADR-0005, ADR-0010). Leaving the variable out is allowed; the service then lacks it."""
    if not previous:
        return
    env = spec.get("env", {})
    for var, value in previous.get("env", {}).items():
        if value == "{generated}" and var in env and env[var] != "{generated}":
            raise ConfigError(
                f"{PROJECT_FILE}: portals.{name}.env.{var} must stay {{generated}}; "
                "Portal secrets cannot be set in project config (ADR-0005, ADR-0010)")


def _validate_portals(portals: Any, label: str, source: str) -> dict[str, dict[str, Any]]:
    portals = _expect_map(portals, label, "portals")
    out = {}
    for name, spec in portals.items():
        if not isinstance(name, str) or not PORTAL_NAME.match(name):
            raise ConfigError(f"{label}: portals.{name}: Portal names must match {PORTAL_NAME.pattern}")
        spec = _expect_map(spec, label, f"portals.{name}")
        _reject_unknown(spec, {"port", "command", "env", "open", "login"}, label, f"portals.{name}.")
        if "port" not in spec:
            raise ConfigError(f"{label}: portals.{name} is missing 'port'")
        _validate_port(spec["port"], label, f"portals.{name}.port")
        if "command" in spec:
            _validate_str_list(spec["command"], label, f"portals.{name}.command")
        if "env" in spec:
            _validate_agent_env(spec["env"], label, f"portals.{name}.env")
        if "login" in spec:
            _validate_login(spec["login"], spec, label, name, source)
        if "open" in spec and (not isinstance(spec["open"], str) or not spec["open"].startswith("/")):
            raise ConfigError(f"{label}: portals.{name}.open must be a URL path starting with '/'")
        out[name] = dict(spec)
    return out


def _validate(raw: Any, label: str, source: str) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{label}: top level must be a mapping")
    if source != "cli":
        if "version" not in raw:
            raise ConfigError(f"{label}: missing required key 'version'")
        if raw["version"] != 1 or isinstance(raw["version"], bool):
            raise ConfigError(f"{label}: unsupported version {raw['version']!r} (expected 1)")
    allowed = {"resources", "secrets", "network", "agents", "portals"}
    allowed |= {"offline"} if source == "cli" else {"version"}
    _reject_unknown(raw, allowed, label)
    out: dict[str, Any] = {}
    if "resources" in raw:
        out["resources"] = _validate_resources(raw["resources"], label)
    if "secrets" in raw:
        out["secrets"] = _validate_secrets(raw["secrets"], label)
    if "network" in raw:
        out["network"] = _validate_network(raw["network"], label, source)
    if "agents" in raw:
        out["agents"] = _validate_agents(raw["agents"], label)
    if "portals" in raw:
        out["portals"] = _validate_portals(raw["portals"], label, source)
    if "offline" in raw:
        if not isinstance(raw["offline"], bool):
            raise ConfigError(f"{label}: offline must be true or false")
        out["offline"] = raw["offline"]
    return out


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e


# --------------------------------------------------------------------------- loading


def default_global_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-sandbox" / "config.yaml"


def load_config(
    workspace: Path,
    *,
    global_path: Path | None = None,
    cli: dict[str, Any] | None = None,
) -> Config:
    """Load and merge all config layers for ``workspace``."""
    global_path = default_global_path() if global_path is None else global_path
    project_path = Path(workspace) / PROJECT_FILE

    layers: list[tuple[str, dict[str, Any]]] = []
    loaded: list[str] = []
    for source, path in (("global", global_path), ("project", project_path)):
        if path.is_file():
            layers.append((source, _validate(_read_yaml(path), str(path), source)))
            loaded.append(str(path))
    if cli:
        layers.append(("cli", _validate(cli, "CLI options", "cli")))

    cfg = Config(
        resources=dict(DEFAULT_RESOURCES),
        resource_sources={k: "default" for k in DEFAULT_RESOURCES},
        secret_mask=[Rule(p, "default") for p in DEFAULT_MASK],
        secret_allow=[Rule(p, "default") for p in DEFAULT_ALLOW],
        secret_skip=[Rule(p, "default") for p in DEFAULT_SKIP],
        host_services=[],
        host_browser=False,
        agents={k: list(v) for k, v in DEFAULT_AGENTS.items()},
        portals=copy.deepcopy(DEFAULT_PORTALS),
        offline=False,
        loaded_files=loaded,
    )

    for source, layer in layers:
        for k, v in layer.get("resources", {}).items():
            cfg.resources[k] = v
            cfg.resource_sources[k] = source
            if source == "project":
                cfg.project_rules.append((f"resources.{k}", str(v)))
        for kind, rules in (("mask", cfg.secret_mask), ("allow", cfg.secret_allow), ("skip", cfg.secret_skip)):
            for pattern in layer.get("secrets", {}).get(kind, []):
                if all(r.pattern != pattern for r in rules):
                    rules.append(Rule(pattern, source))
                if source == "project":
                    cfg.project_rules.append((f"secrets.{kind}", pattern))
        net = layer.get("network", {})
        for svc in net.get("host_services", []):
            cfg.host_services = [s for s in cfg.host_services if s["name"] != svc["name"]] + [svc]
            if source == "project":
                cfg.project_rules.append(("network.host_services", f"{svc['name']} {svc['host']}:{svc['port']}"))
        if "host_browser" in net:
            cfg.host_browser = net["host_browser"]
        for name, spec in layer.get("agents", {}).items():
            cfg.agents[name] = spec["command"]
            cfg.agent_env.pop(name, None)  # the entry is replaced as a whole, env included
            if spec.get("env"):
                cfg.agent_env[name] = dict(spec["env"])
            if source == "project":
                cfg.project_rules.append((f"agents.{name}", " ".join(spec["command"])))
                for key in spec.get("env", {}):  # names only: values may be tokens
                    cfg.project_rules.append((f"agents.{name}.env", key))
        for name, spec in layer.get("portals", {}).items():
            if source == "project":
                _keep_generated(cfg.portals.get(name), spec, name)
            cfg.portals[name] = spec
            if source == "project":
                cfg.project_rules.append((f"portals.{name}", f"port {spec['port']}"))
                if spec.get("command"):  # a project can make the Session run a program: show it
                    cfg.project_rules.append((f"portals.{name}.command", " ".join(spec["command"])))
                for key in spec.get("env", {}):  # names only: values may be tokens
                    cfg.project_rules.append((f"portals.{name}.env", key))
        if "offline" in layer:
            cfg.offline = layer["offline"]
    return cfg
