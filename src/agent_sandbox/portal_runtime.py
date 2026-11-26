"""Start, stop and inspect Portals in a running Session (ADR-0010, ADR-0013, ADR-0018).

Everything here runs against the live Session: neither the Session nor Netguard is restarted or
replaced. A start that fails part-way undoes every step it did, so a failed Portal is never left
published, and never leaves a rule or a network behind.

Start, in order (each step is undone by `_unpublish` if a later one fails):
  1. optional: run the configured `command` in the Session and wait until the port answers
  2. create the Portal's own `--internal` network and connect Netguard to it
  3. run the publisher (127.0.0.1:<random>:8000 -> Netguard's address:<port>) and connect it
  4. in Netguard: drop the Portal network for anything the Session starts (NG_LOCAL), then admit
     that publisher to that one port (INPUT); read both back
  5. read the host binding back from Docker and refuse anything that is not 127.0.0.1
"""
from __future__ import annotations

import json
import shlex
import time
from pathlib import Path
from typing import TextIO

from . import generated, portals
from .plan import SessionPlan
from .runtime import Docker, DockerError

READY_TIMEOUT = 15.0  # seconds a Portal command gets to start listening
READY_POLL = 0.2


def _ng(docker: Docker, netguard: str, *iptables: str, check: bool = True):
    return docker.run("exec", netguard, "iptables", "-w", *iptables, check=check)


def _find(plan: SessionPlan, name: str) -> dict:
    if not plan.netguard_name:
        raise portals.PortalError("offline Session: it has no network, so no Portals")
    for e in plan.portals:
        if e["name"] == name:
            return e
    known = ", ".join(e["name"] for e in plan.portals) or "none configured"
    raise portals.PortalError(f"unknown Portal '{name}' (configured: {known})")


def _require_running(docker: Docker, plan: SessionPlan) -> None:
    if not docker.is_running(plan.container_name):
        raise portals.PortalError(f"no running Session for {plan.workspace}; run `asb up` first")
    if not docker.container_running(plan.netguard_name):
        raise portals.PortalError(
            f"Netguard {plan.netguard_name} is not running: the Session is offline (--offline) or lost its "
            "network, and Portals need Netguard; run `asb down` and `asb up`")


def _network_ips(docker: Docker, container: str, network: str) -> str:
    out = docker.run("inspect", "--format", "{{json .NetworkSettings.Networks}}", container).out
    try:
        ip = json.loads(out)[network]["IPAddress"]
    except (ValueError, KeyError, TypeError) as e:
        raise portals.PortalError(f"{container} has no address on {network}") from e
    if not ip:
        raise portals.PortalError(f"{container} has no address on {network}")
    return ip


class PortalController:
    def __init__(self, docker: Docker, plan: SessionPlan):
        self.d = docker
        self.plan = plan

    # -- state
    def published(self, entry: dict) -> bool:
        return self.d.container_running(entry["publisher"])

    def host_port(self, entry: dict) -> int:
        r = self.d.run("port", entry["publisher"], f"{portals.PUBLISH_PORT}/tcp", check=False)
        if r.code != 0:
            raise portals.PortalError(f"Docker reports no published port for {entry['name']}")
        return portals.parse_published(r.out)

    def url(self, entry: dict) -> str:
        return portals.url(self.host_port(entry), entry["open"])

    # -- start
    def start(self, name: str) -> str:
        entry = _find(self.plan, name)
        _require_running(self.d, self.plan)
        if self.published(entry):
            return self.url(entry)  # already on: same URL, nothing re-created
        if not self.d.has_image(portals.IMAGE):
            raise portals.PortalError(f"image {portals.IMAGE} not found; run `asb build` first")
        env = self._env(entry)  # may refuse (secret store inside the Workspace) before anything starts
        self._unpublish(entry)  # leftovers of a crashed start
        started_process = bool(entry["command"])
        try:
            if started_process:
                self._start_process(entry, env)
            self._publish(entry)
            return self.url(entry)
        except (DockerError, portals.PortalError) as e:
            self._unpublish(entry)
            if started_process:
                self._stop_process(entry)
            if isinstance(e, portals.PortalError):
                raise
            raise portals.PortalError(f"could not start Portal {name}: {e}") from e

    def _publish(self, entry: dict) -> None:
        session, ng = self.plan.container_name, self.plan.netguard_name
        self.d.run(*portals.network_create(entry, session))
        self.d.run("network", "connect", entry["network"], ng)
        ng_ip = _network_ips(self.d, ng, entry["network"])
        subnet = self.d.run("network", "inspect", "--format", "{{(index .IPAM.Config 0).Subnet}}", entry["network"]).out.strip()
        if not subnet:
            raise portals.PortalError(f"cannot read the subnet of {entry['network']}")
        # The Session shares Netguard's network namespace, so it sees this new interface too.
        # Drop that subnet for everything the Session starts *before* anything else lives on it.
        self._rule("-A", portals.local_drop_rule(subnet))
        self.d.run(*portals.publisher_run(entry, session, ng_ip))
        self.d.run("network", "connect", entry["network"], entry["publisher"])
        self._rule("-A", portals.input_rule(entry, _network_ips(self.d, entry["publisher"], entry["network"])))
        self.host_port(entry)  # raises unless Docker reports 127.0.0.1 only

    def _rule(self, op: str, rule: list[str]) -> None:
        """Apply one Netguard rule and read it back; fail closed if it is not there."""
        _ng(self.d, self.plan.netguard_name, op, *rule)
        _ng(self.d, self.plan.netguard_name, "-C", *rule)

    def _unpublish(self, entry: dict) -> None:
        """Remove everything a start creates. Safe to call at any point, and twice."""
        ng = self.plan.netguard_name
        pub_ip = subnet = None
        if self.d.run("network", "inspect", entry["network"], check=False).code == 0:
            subnet = self.d.run("network", "inspect", "--format", "{{(index .IPAM.Config 0).Subnet}}",
                                entry["network"], check=False).out.strip() or None
            if self.d.container_running(entry["publisher"]):
                try:
                    pub_ip = _network_ips(self.d, entry["publisher"], entry["network"])
                except portals.PortalError:
                    pass
        if pub_ip:
            _ng(self.d, ng, "-D", *portals.input_rule(entry, pub_ip), check=False)
        else:
            tag = f"{portals.RULE_TAG}{entry['name']}"
            r = _ng(self.d, ng, "-S", "INPUT", check=False)
            for line in r.out.splitlines():
                parts = shlex.split(line)
                if len(parts) >= 2 and parts[0] == "-A" and parts[1] == "INPUT" and tag in parts:
                    _ng(self.d, ng, "-D", *parts[1:], check=False)
        if subnet:
            _ng(self.d, ng, "-D", *portals.local_drop_rule(subnet), check=False)
        self.d.run("rm", "-f", entry["publisher"], check=False)
        self.d.run("network", "disconnect", "-f", entry["network"], ng, check=False)
        self.d.run("network", "rm", entry["network"], check=False)

    # -- the service the CLI started through `command`
    def _env(self, entry: dict) -> dict[str, str]:
        """The Portal's env with `{generated}` values filled in (created once per Workspace, ADR-0020)."""
        return generated.resolve(self.plan.container_name, Path(self.plan.workspace), entry["name"], entry["env"])

    def login(self, entry: dict) -> dict | None:
        """The username and password to show for a Portal with a `login`, and where they are kept."""
        if not entry.get("login") or not generated.names(entry["env"]):
            return None
        env = self._env(entry)
        return {"username": env[entry["login"]["username"]], "password": env[entry["login"]["password"]],
                "store": str(generated.store_path(self.plan.container_name))}

    def _start_process(self, entry: dict, env: dict[str, str]) -> None:
        session = self.plan.container_name
        secret = {k: env[k] for k in generated.names(entry["env"])}
        # literal values go on the command line; generated ones as a bare `-e NAME` whose value
        # `docker` reads from its own environment, so `ps` on the host never shows them
        flags = [a for k, v in sorted(env.items()) for a in ("-e", k if k in secret else f"{k}={v}")]
        self.d.run("exec", "-d", "-w", "/workspace", *flags, session, "sh", "-c", portals.START_SCRIPT,
                   portals.pidfile(entry["name"]), *entry["command"], env=secret)
        deadline = time.monotonic() + READY_TIMEOUT
        while not self._listening(entry):
            if time.monotonic() >= deadline:
                raise portals.PortalError(
                    f"Portal {entry['name']}: its command did not listen on port {entry['port']} "
                    f"within {READY_TIMEOUT:g}s; nothing was published")
            time.sleep(READY_POLL)

    def _stop_process(self, entry: dict) -> None:
        self.d.run("exec", self.plan.container_name, "sh", "-c", portals.STOP_SCRIPT, portals.pidfile(entry["name"]), check=False)

    def _listening(self, entry: dict) -> bool:
        return self.d.run("exec", self.plan.container_name, "bash", "-c", portals.LISTEN_SCRIPT,
                          str(entry["port"]), check=False).code == 0

    def _process_alive(self, entry: dict) -> bool:
        return self.d.run("exec", self.plan.container_name, "sh", "-c", portals.ALIVE_SCRIPT,
                          portals.pidfile(entry["name"]), check=False).code == 0

    # -- stop, restart, status
    def stop(self, name: str) -> bool:
        """Unpublish. Stops the process only when the CLI started it. Returns whether it was published."""
        entry = _find(self.plan, name)
        was = self.published(entry)
        self._unpublish(entry)
        if entry["command"] and self.d.is_running(self.plan.container_name):
            self._stop_process(entry)
        return was

    def restart(self, name: str) -> str:
        self.stop(name)
        return self.start(name)

    def status(self, name: str) -> dict:
        entry = _find(self.plan, name)
        _require_running(self.d, self.plan)
        published = self.published(entry)
        out = {
            "name": name, "published": published,
            "url": self.url(entry) if published else None,
            "listening": self._listening(entry),
            "started_by": entry["started_by"],
        }
        if entry["command"]:
            out["process"] = "running" if self._process_alive(entry) else "not running"
        return out


def render_status(s: dict) -> str:
    lines = [f"Portal {s['name']}",
             "  publication: " + (f"published at {s['url']}" if s["published"] else "not published")]
    service = "listening" if s["listening"] else "not listening"
    if s["started_by"] == "cli":
        lines.append(f"  service:     {service} (command started by asb: {s['process']})")
    else:
        lines.append(f"  service:     {service} (started by the agent; asb does not manage it)")
    if s["published"] and not s["listening"]:
        lines.append("  note:        publication alone does not prove the service is ready")
    return "\n".join(lines)


# -- whole-Session helpers used by `down`, `up` and `ls`


def teardown_all(docker: Docker, plan: SessionPlan) -> None:
    """Remove every publisher and Portal network of this Session, whatever the config says now."""
    session = plan.container_name
    r = docker.run("ps", "-a", "--filter", f"label={portals.LABEL}", "--filter", f"label={portals.SESSION_LABEL}={session}",
                   "--format", "{{.Names}}", check=False)
    for name in r.out.split():
        docker.run("rm", "-f", name, check=False)
    r = docker.run("network", "ls", "--filter", "label=agent-sandbox=portal-network",
                   "--filter", f"label={portals.SESSION_LABEL}={session}", "--format", "{{.Name}}", check=False)
    for net in r.out.split():
        if plan.netguard_name:
            docker.run("network", "disconnect", "-f", net, plan.netguard_name, check=False)
        docker.run("network", "rm", net, check=False)


def active_urls(docker: Docker, session: str) -> list[tuple[str, str]]:
    """(name, URL) of each published Portal of a Session; one Docker cannot vouch for is left out."""
    fmt = '{{.Names}}\t{{.Label "%s"}}\t{{.Label "%s"}}' % (portals.NAME_LABEL, portals.OPEN_LABEL)
    r = docker.run("ps", "--filter", f"label={portals.LABEL}", "--filter", f"label={portals.SESSION_LABEL}={session}",
                   "--format", fmt, check=False)
    out = []
    for line in sorted(r.out.splitlines()):
        pub, name, path = (line.split("\t") + ["", ""])[:3]
        p = docker.run("port", pub, f"{portals.PUBLISH_PORT}/tcp", check=False)
        try:
            out.append((name, portals.url(portals.parse_published(p.out), path)))
        except portals.PortalError:
            continue
    return out
