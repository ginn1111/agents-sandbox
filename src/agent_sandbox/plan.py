"""The Session plan: a pure function from (workspace, config) to a SessionPlan.

No Docker calls and no writes happen here. ``asb check`` renders the plan,
``asb check --json`` serialises it and the runtime executes it.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import host_browser as _hb
from . import host_services as _hs
from .config import Config
from .netguard import IMAGE as NETGUARD_IMAGE
from .netguard import RULES_ENV, rules as netguard_rules
from .discovery import Entry, discover
from .portals import plan_entries as portal_entries
from .kanban import plan_entry as kanban_entry

IMAGE = "agent-sandbox-agent:latest"
FALLBACK_UID = 1000

# Agent volumes (ADR-0006): one per agent, named after the agent only, so the same
# volume is reused by every Workspace and never by another agent.
AGENT_HOME_ROOT = "/agents"
MISE_VOLUME = "agent-sandbox-mise"
MISE_DIR = "/mise"
# Agents the image ships and pre-creates a writable home for (see images/agent/Dockerfile).
IMAGE_AGENTS = frozenset({"hermes", "omp", "claude", "codex"})


def agent_volume(name: str) -> str:
    # Retain shipped login volumes. A dot separates custom Agent volumes from
    # legacy built-ins and the mise/Kanban namespaces (agent names cannot contain dots).
    return f"agent-sandbox-{name}" if name in IMAGE_AGENTS else f"agent-sandbox-agent.{name}"


def agent_home(name: str) -> str:
    return f"{AGENT_HOME_ROOT}/{name}"

# SPEC section 11. Shown by `check` and kept in sync with the README.
LIMITS = [
    "Masking is by filename only; secrets inside other files are not detected.",
    "Secrets committed to git history stay readable.",
    "Files created after `up`, or under Skip patterns, are not masked.",
    "Hard links are found only by scanning the Workspace outside Skip patterns; a hard link under a Skip pattern or outside the Workspace is not masked.",
    "The agent can change Workspace files that later run on the host (Makefile, package.json, .git/hooks).",
    "Host Services can return sensitive data; the Host Browser, when enabled, exposes the host's whole network view and host files.",
    "The public internet stays open over IPv4, including public IP addresses on the host or LAN. Netguard does not prevent data exfiltration.",
    "Containers share the host kernel.",
    "Agent credentials in Agent volumes are readable by the agent and by anything it runs, including other processes in the same Session. Writable Agent and mise volumes are shared across Workspaces and can carry changes into later Sessions.",
    "The inner herdr can set the terminal title and request clipboard writes (OSC 52); turn off clipboard writes from programs in your terminal.",
]


@dataclass
class SessionPlan:
    workspace: str
    container_name: str
    image: str
    config_files: list[str]
    project_rules: list[tuple[str, str]]
    session_run: list[str]
    warnings: list[str] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    mask_sources: dict[str, str] = field(default_factory=dict)  # kind (file|dir) -> empty read-only source
    errors: list[str] = field(default_factory=list)
    # One entry per enabled agent. Only runtime uses env values; public plans list env names.
    agents: list[dict] = field(default_factory=list)
    # Named volumes the Session mounts; `up` creates them and `down` never removes them.
    volumes: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=lambda: list(LIMITS))
    result: str = "Ready"
    netguard_name: str | None = None
    netguard_image: str | None = None
    netguard_run: list[str] | None = None
    netguard_rules: list[str] = field(default_factory=list)
    # Configured Host Services (ADR-0002, ADR-0013): available, never active after `up`.
    host_services: list[dict] = field(default_factory=list)
    # One entry per configured Portal (ADR-0010). Nothing is published by `up`; `asb portal start`
    # publishes one on 127.0.0.1 (ADR-0013, ADR-0018).
    portals: list[dict] = field(default_factory=list)
    # This Workspace's Hermes Kanban board, its own volume and env (ADR-0020).
    kanban: dict = field(default_factory=dict)
    # The Host Browser (ADR-0008, ADR-0013): None when off; otherwise available but never active after `up`.
    host_browser: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for agent in data["agents"]:
            agent["env"] = sorted(agent["env"])
        return data


def session_name(workspace: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-") or "workspace"
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:8]
    return f"asb-{slug[:30]}-{digest}"


def _csv(field_: str) -> str:
    """One field of a docker --mount value: quoted only when it holds a comma, quote or newline."""
    if any(c in field_ for c in ',"\n\r'):
        return '"' + field_.replace('"', '""') + '"'
    return field_


def _mount_arg(source: Path | str, target: str, readonly: bool = False) -> str:
    quoted = str(source).replace('"', '""')
    arg = f'type=bind,"source={quoted}",{_csv("target=" + target)}'
    return arg + ",readonly" if readonly else arg


def mask_source_dir() -> Path:
    """Where the empty read-only mask sources live. Never inside a Workspace."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "agent-sandbox" / "masks"


def _volume_mount_arg(volume: str, target: str) -> str:
    return f"type=volume,source={volume},target={target}"


def _agent_entry(name: str, command: list[str], env: dict[str, str]) -> dict:
    home = agent_home(name)
    launch = ["/opt/asb/asb-agent", name]
    return {
        "name": name, "command": list(command), "env": dict(env),
        "volume": agent_volume(name), "home": home, "launch": launch,
    }


def _mask_mounts(entries: list[Entry], sources: dict[str, str]) -> list[str]:
    args: list[str] = []
    for e in entries:
        if e.status == "MASKED":
            target = "/workspace/" + e.path
            args += ["--mount", _mount_arg(sources[e.kind], target, readonly=True)]
    return args


def _netguard_run(name: str, workspace: Path, platform: str) -> list[str]:
    """`docker run` for the Netguard container (ADR-0003).

    It alone holds NET_ADMIN. It runs as root because a non-root process gets no effective
    capabilities, so iptables could not run; the Session is the non-root one. Everything Docker
    refuses on a container that joins another's network (hostname, add-host, sysctls, published
    ports) is set here.
    """
    args = [
        "run", "-d",
        "--name", name,
        "--hostname", "asb",
        "--label", "agent-sandbox=netguard",
        "--label", f"agent-sandbox.workspace={workspace}",
        "--read-only",
        "--tmpfs", "/run",
        "--user", "0:0",
        "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
        "--security-opt", "no-new-privileges",
        "--cpus", "0.5", "--memory", "64m", "--pids-limit", "64",
        "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
        "--sysctl", "net.ipv6.conf.default.disable_ipv6=1",
        "--env", RULES_ENV + "=" + ";".join(netguard_rules()),
    ]
    if platform == "linux":  # Docker Desktop and Colima resolve this name themselves
        args += ["--add-host", "host.docker.internal:host-gateway"]
    return args + [NETGUARD_IMAGE]


def plan(workspace: Path, config: Config, *, uid: int | None = None, gid: int | None = None,
         platform: str | None = None) -> SessionPlan:
    workspace = Path(workspace).resolve()
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    if uid == 0:  # never run the agent as root, even when the host user is root
        uid, gid = FALLBACK_UID, FALLBACK_UID
    name = session_name(workspace)
    platform = sys.platform if platform is None else platform
    ng_name = None if config.offline else f"{name}-netguard"

    found = discover(workspace, config)
    kinds = {e.kind for e in found.entries if e.status == "MASKED"}
    root = mask_source_dir()
    sources = {"file": str(root / "empty-file"), "dir": str(root / "empty-dir")}
    sources = {k: v for k, v in sources.items() if k in kinds}
    errors = list(found.errors)
    real_root = Path(os.path.realpath(root))
    if sources and (real_root == workspace or workspace in real_root.parents):
        # an agent could otherwise fill the "empty" mask source through /workspace
        errors.append(f"mask sources ({root}) are inside the Workspace; set XDG_STATE_HOME elsewhere")

    agents = [_agent_entry(n, config.agents[n], config.agent_env.get(n, {})) for n in sorted(config.agents)]
    portal_list, portal_env, portal_errors = portal_entries(name, config.portals, config.offline)
    errors += portal_errors
    kb = kanban_entry(name, workspace)
    volumes = sorted({a["volume"] for a in agents} | {MISE_VOLUME, kb["volume"]})
    volume_mounts: list[str] = []
    for a in agents:
        volume_mounts += ["--mount", _volume_mount_arg(a["volume"], a["home"])]
    volume_mounts += ["--mount", _volume_mount_arg(MISE_VOLUME, MISE_DIR)]
    volume_mounts += ["--mount", _volume_mount_arg(kb["volume"], kb["home"])]

    args = [
        "run", "-d",
        "--name", name,
        *(["--hostname", "asb"] if ng_name is None else []),  # a joined network namespace owns it
        "--label", "agent-sandbox=session",
        "--label", f"agent-sandbox.workspace={workspace}",
        "--read-only",
        "--init",  # reaps the processes herdr panes leave behind; adds no capability
        "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--cpus", str(config.resources["cpus"]),
        "--memory", str(config.resources["memory"]),
        "--pids-limit", str(config.resources["pids_limit"]),
        "--network", "none" if ng_name is None else f"container:{ng_name}",
        "--env", "HOME=/tmp",
        *(a for k, v in sorted({**portal_env, **kb["env"]}.items()) for a in ("--env", f"{k}={v}")),
        "--mount", _mount_arg(workspace, "/workspace"),
        *_mask_mounts(found.entries, sources),
        *volume_mounts,
        "-w", "/workspace",
        IMAGE,
    ]

    warnings = list(found.warnings)
    if any(a["env"] for a in agents):
        warnings.append("Configured Agent environment values are not isolated from other processes in the same Session.")
    for a in agents:
        if a["name"] not in IMAGE_AGENTS:
            warnings.append(
                f"agent '{a['name']}' is not installed in the image, and Docker creates its volume "
                f"{a['volume']} root-owned, so {a['home']} may not be writable; add the agent to the "
                "image Dockerfile (ADR-0007)"
            )

    if ng_name is None and config.host_services:
        warnings.append("offline: the configured Host Services cannot be reached (there is no network)")
    if config.offline and config.portals:
        warnings.append("offline: the Session has no network, so Portals are unavailable")

    browser, browser_warnings, browser_errors = _hb.plan_entry(config.host_browser, ng_name, workspace)
    warnings += browser_warnings
    errors += browser_errors

    return SessionPlan(
        workspace=str(workspace),
        container_name=name,
        image=IMAGE,
        config_files=list(config.loaded_files),
        project_rules=list(config.project_rules),
        session_run=args,
        warnings=warnings,
        netguard_name=ng_name,
        netguard_image=None if ng_name is None else NETGUARD_IMAGE,
        netguard_run=None if ng_name is None else _netguard_run(ng_name, workspace, platform),
        netguard_rules=[] if ng_name is None else netguard_rules(),
        entries=found.entries,
        mask_sources=sources,
        errors=errors,
        result="Blocked" if errors else "Ready",
        agents=agents,
        volumes=volumes,
        host_services=_hs.plan_entries(config.host_services, ng_name),
        portals=portal_list,
        kanban=kb,
        host_browser=browser,
    )
