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

from .config import Config
from .netguard import IMAGE as NETGUARD_IMAGE
from .netguard import RULES_ENV, rules as netguard_rules

IMAGE = "agent-sandbox-agent:latest"
FALLBACK_UID = 1000

# SPEC section 11. Shown by `check` and kept in sync with the README.
LIMITS = [
    "Masking is by filename only; secrets inside other files are not detected.",
    "Secrets committed to git history stay readable.",
    "Files created after `up`, or under Skip patterns, are not masked.",
    "The agent can change Workspace files that later run on the host (Makefile, package.json, .git/hooks).",
    "Host Services can return sensitive data; the Host Browser, when enabled, exposes the host's whole network view and host files.",
    "The public internet stays open: the agent can send Workspace data to any public IP address (Netguard blocks the host and private networks, not exfiltration).",
    "Containers share the host kernel.",
    "Agent credentials in Agent volumes are readable by the agent and by anything it runs.",
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
    limits: list[str] = field(default_factory=lambda: list(LIMITS))
    result: str = "Ready"
    netguard_name: str | None = None
    netguard_image: str | None = None
    netguard_run: list[str] | None = None
    netguard_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def session_name(workspace: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-") or "workspace"
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:8]
    return f"asb-{slug[:30]}-{digest}"


def _mount_arg(source: Path, target: str) -> str:
    quoted = str(source).replace('"', '""')
    return f'type=bind,"source={quoted}",target={target}'


def _external_git_warnings(workspace: Path) -> list[str]:
    git = workspace / ".git"
    if not git.is_file():
        return []
    try:
        first = git.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        first = ""
    if first.startswith("gitdir:"):
        target = (workspace / first.split(":", 1)[1].strip()).resolve()
        if workspace not in (target, *target.parents):
            return [
                f".git points to {target}, outside the Workspace; it is not mounted, "
                "so git will not work inside the Session"
            ]
    return []


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

    args = [
        "run", "-d",
        "--name", name,
        *(["--hostname", "asb"] if ng_name is None else []),  # a joined network namespace owns it
        "--label", "agent-sandbox=session",
        "--label", f"agent-sandbox.workspace={workspace}",
        "--read-only",
        "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--cpus", str(config.resources["cpus"]),
        "--memory", str(config.resources["memory"]),
        "--pids-limit", str(config.resources["pids_limit"]),
        "--network", "none" if ng_name is None else f"container:{ng_name}",
        "--env", "HOME=/tmp",
        "--mount", _mount_arg(workspace, "/workspace"),
        "-w", "/workspace",
        IMAGE,
    ]

    warnings = [
        "Secret files are not masked yet: every file in the Workspace, including .env and "
        ".git/config, is readable and writable by the agent.",
    ]
    warnings += _external_git_warnings(workspace)

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
    )
