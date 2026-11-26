"""Portals (ADR-0010, ADR-0013, ADR-0018): the plan entries and the live publish/unpublish steps.

A Portal is a config entry: a container port, optional env, optional background command and a URL
path. Nothing here knows any one tool. Plain `asb up` publishes nothing (ADR-0013); `asb portal
start <name>` publishes one Portal on `127.0.0.1:<random host port>` without restarting the
Session or Netguard.

Docker cannot add a published port to a running container, and the Session shares Netguard's
network namespace. So every Portal gets its own *publisher*: a small socat container (its
own small image: no capabilities, no mounts, no env) that publishes ``127.0.0.1::8000`` and forwards
to the Portal port on Netguard's address on a per-Portal *portal network*. Netguard is
connected to that network live, and one INPUT rule admits only that publisher to that one port.
The Session cannot start connections to the publisher: Netguard drops private ranges and the
portal network's subnet. Traffic starts on the host and goes in, never out. Each Portal has its own
network, so stopping one touches nothing else.
"""
from __future__ import annotations

import re
from typing import Any

IMAGE = "agent-sandbox-portal:latest"

PUBLISH_PORT = 8000  # the publisher's own listening port; Docker maps it to a random loopback port
LOOPBACK = "127.0.0.1"
LABEL = "agent-sandbox=portal"
SESSION_LABEL = "agent-sandbox.session"
NAME_LABEL = "agent-sandbox.portal"
OPEN_LABEL = "agent-sandbox.open"
RULE_TAG = "asb-portal:"  # comment on the Netguard INPUT rule of one Portal


class PortalError(RuntimeError):
    """A Portal could not be started, stopped or inspected."""


# --------------------------------------------------------------------------- plan (pure)


def publisher_name(session: str, portal: str) -> str:
    return f"{session}-portal-{portal}"


def network_name(session: str, portal: str) -> str:
    return f"{session}-portal-{portal}-net"


def pidfile(portal: str) -> str:
    return f"/tmp/asb-portal-{portal}.pid"


def plan_entries(session: str, portals: dict[str, dict[str, Any]], offline: bool) -> tuple[list[dict], dict[str, str], list[str]]:
    """(entries, Session env, errors) for the configured Portals.

    Env of a Portal the agent starts is set on the Session, because the agent's own process needs
    it (Plannotator reads it from the environment of its plan hook). Env of a Portal with a
    `command` is given only to that command when `portal start` runs it. Offline Sessions have no
    network, so no Portals.
    """
    if offline:
        return [], {}, []
    entries, env, errors, ports = [], {}, [], {}
    for name in sorted(portals):
        spec = portals[name]
        command = list(spec["command"]) if spec.get("command") else None
        entry = {
            "name": name, "port": spec["port"], "open": spec.get("open", "/"),
            "command": command, "env": dict(spec.get("env", {})),
            "started_by": "cli" if command else "agent",
            "publisher": publisher_name(session, name), "network": network_name(session, name),
            "publish": f"{LOOPBACK}::{PUBLISH_PORT}",
        }
        entries.append(entry)
        if spec["port"] in ports:
            errors.append(f"portals {ports[spec['port']]} and {name} use the same port {spec['port']}")
        ports[spec["port"]] = name
        if not command:
            for k, v in entry["env"].items():
                if env.get(k, v) != v:
                    errors.append(f"portals set {k} to different values; Portals the agent starts share the Session environment")
                env[k] = v
    return entries, env, errors


def url(host_port: int, path: str | None) -> str:
    return f"http://{LOOPBACK}:{host_port}{path or '/'}"


def parse_published(out: str) -> int:
    """The host port from `docker port` output, accepted only if every binding is on 127.0.0.1."""
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    if not lines:
        raise PortalError("Docker reports no published port")
    ports = set()
    for line in lines:
        m = re.fullmatch(r"127\.0\.0\.1:(\d+)", line)
        if not m:
            raise PortalError(f"Portal is published on {line!r}, not only on {LOOPBACK}; refusing to keep it")
        ports.add(int(m.group(1)))
    if len(ports) != 1:
        raise PortalError("Portal is published on more than one host port; refusing to keep it")
    return ports.pop()


def publisher_run(entry: dict, session: str, netguard_ip: str) -> list[str]:
    """`docker run` for one Portal's publisher: no capabilities, no mounts, no env, loopback publish.

    It starts on Docker's default bridge, which is where the host port is published. The CLI then
    connects it to the Portal's internal network, which is how it reaches Netguard.
    """
    return [
        "run", "-d",
        "--name", entry["publisher"],
        "--label", LABEL,
        "--label", f"{SESSION_LABEL}={session}",
        "--label", f"{NAME_LABEL}={entry['name']}",
        "--label", f"{OPEN_LABEL}={entry['open']}",
        "--publish", entry["publish"],
        "--read-only",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--cpus", "0.25", "--memory", "32m", "--pids-limit", "64",
        IMAGE,
        f"TCP-LISTEN:{PUBLISH_PORT},fork,reuseaddr",
        f"TCP:{netguard_ip}:{entry['port']}",
    ]


def network_create(entry: dict, session: str) -> list[str]:
    """The Portal's own network. `--internal`: no gateway (Netguard's default route stays on its
    own network) and nothing on it can reach out to the host or the internet."""
    return [
        "network", "create", "--internal",
        "--label", "agent-sandbox=portal-network",
        "--label", f"{SESSION_LABEL}={session}",
        entry["network"],
    ]


# iptables rules inside Netguard, as argument lists for `docker exec <netguard> iptables ...`.


def input_rule(entry: dict, publisher_ip: str) -> list[str]:
    """The one INPUT rule for a Portal: that publisher, that port, nothing else."""
    return ["INPUT", "-p", "tcp", "-s", publisher_ip, "--dport", str(entry["port"]), "-j", "ACCEPT"]


def local_drop_rule(subnet: str) -> list[str]:
    """Nothing in the Session may start a connection into the Portal network (NG_LOCAL, ADR-0002)."""
    return ["NG_LOCAL", "-d", subnet, "-j", "DROP"]


# Commands run inside the Session for a Portal that has a `command`.
START_SCRIPT = 'echo $$ > "$0" && exec "$@"'
ALIVE_SCRIPT = 'kill -0 "$(cat "$0")"'
STOP_SCRIPT = 'kill "$(cat "$0")"; rm -f "$0"'
LISTEN_SCRIPT = 'exec 3<>/dev/tcp/127.0.0.1/"$0"'
