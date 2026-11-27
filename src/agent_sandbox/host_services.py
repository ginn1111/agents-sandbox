"""Host Services (ADR-0002, ADR-0013, ADR-0016): validation, plan entries and the enable/disable commands.

A Host Service is a ``host:port`` pair the Session may open TCP connections to. Configuration only
makes it *available*; nothing is opened by ``up``. ``asb service enable <name>`` runs the Netguard
helper ``asb-service`` (``images/netguard/service.sh``) with ``docker exec`` inside the running
Netguard. The helper appends ACCEPT rules to the ``NG_SERVICES`` chain, which is consulted before
the range drops, so the listed port is reachable and every other host port stays dropped. The rules
live in Netguard's network namespace: they end with the Session, and no host credential is passed.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .plan import SessionPlan
    from .runtime import Docker

CHAIN = "NG_SERVICES"
HELPER = "/usr/local/bin/asb-service"
# Service names are CLI arguments and state file names inside Netguard, so keep them plain.
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_IPV4 = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
PROBE_SECONDS = 3


class ServiceError(RuntimeError):
    """A Host Service could not be enabled or disabled."""


def host_error(host: str) -> str | None:
    """Why ``host`` cannot be a Host Service host, or None. DNS names and IPv4 addresses only (IPv6 is off).

    Loopback, unspecified, multicast, reserved, link-local (cloud metadata) and broadcast addresses
    are never a host app and would only widen what the Session can reach, so they are refused.
    """
    if _IPV4.match(host):
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            return "is not a valid IPv4 address"
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local:
            return "must not be a loopback, unspecified, multicast, reserved, link-local or broadcast address"
        return None
    if not _HOSTNAME.match(host) or ".." in host:
        return "must be a DNS name or an IPv4 address (no scheme, port or path)"
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return "must not be localhost, which is the Session's own loopback; use host.docker.internal for the host"
    return None


def name_error(name: str) -> str | None:
    if not NAME.match(name):
        return "must match " + NAME.pattern + " (lowercase letters, digits and dashes)"
    return None


def rule(service: dict[str, Any]) -> str:
    """The NG_SERVICES rule `enable` adds: new TCP connections to this host and port, nothing else.

    A DNS name is resolved to IPv4 addresses by the helper when the service is enabled and one rule
    is added per address. Replies need no rule (conntrack RELATED,ESTABLISHED).
    """
    return f"-A {CHAIN} -d {service['host']} -p tcp --dport {service['port']} -j ACCEPT"


def helper_argv(netguard: str, action: str, *args: str) -> list[str]:
    """`docker` arguments that run the helper as Netguard's own (root, NET_ADMIN only) process."""
    return ["exec", netguard, HELPER, action, *args]


def plan_entries(services: list[dict[str, Any]], netguard: str | None) -> list[dict[str, Any]]:
    """One entry per configured Host Service for the SessionPlan. None is active after `up`."""
    if netguard is None:  # --offline: there is no network to open a path through
        return []
    return [
        {
            "name": s["name"], "host": s["host"], "port": s["port"],
            "rule": rule(s),
            "enable": helper_argv(netguard, "enable", s["name"], s["host"], str(s["port"])),
            "disable": helper_argv(netguard, "disable", s["name"]),
            "active": False,
        }
        for s in sorted(services, key=lambda s: s["name"])
    ]


def _running_netguard(docker: "Docker", plan: "SessionPlan") -> str:
    if not docker.is_running(plan.container_name):
        raise ServiceError(f"no Session is running for {plan.workspace}; run `asb up` first")
    if not docker.container_running(plan.netguard_name):
        raise ServiceError(
            f"Netguard {plan.netguard_name} has stopped, so the Session has no network; "
            "run `asb down` and then `asb up`")
    return plan.netguard_name


def _offline() -> ServiceError:
    return ServiceError("this Session is offline: there is no network, so no Host Service can be reached")


def enable(docker: "Docker", plan: "SessionPlan", name: str) -> dict[str, Any]:
    """Open one configured Host Service in the running Session's Netguard. Nothing else changes."""
    if plan.netguard_name is None:
        raise _offline()
    entry = next((e for e in plan.host_services if e["name"] == name), None)
    if entry is None:
        configured = ", ".join(e["name"] for e in plan.host_services) or "none"
        raise ServiceError(f"Host Service '{name}' is not configured (configured: {configured})")
    _running_netguard(docker, plan)
    docker.run(*entry["enable"])  # fails closed: a failed helper opens nothing and raises DockerError
    return entry


def disable(docker: "Docker", plan: "SessionPlan", name: str) -> None:
    """Close a Host Service. Allowed for a name that has left the config, so nothing stays open."""
    if plan.netguard_name is None:
        raise _offline()
    if name_error(name):
        raise ServiceError(f"Host Service name '{name}' {name_error(name)}")
    docker.run(*helper_argv(_running_netguard(docker, plan), "disable", name))


def enabled(docker: "Docker", plan: "SessionPlan") -> set[str]:
    """Names currently open in the Session's Netguard (empty when no Session is running)."""
    if plan.netguard_name is None or not docker.is_running(plan.container_name) \
            or not docker.container_running(plan.netguard_name):
        return set()
    r = docker.run(*helper_argv(plan.netguard_name, "list"), check=False)
    return set(r.out.split()) if r.code == 0 else set()


def probe_from_session(docker: "Docker", plan: "SessionPlan", entry: dict[str, Any]) -> bool:
    """Can the Session open a TCP connection to the service? bash's /dev/tcp, because the image has no nc."""
    return docker.run(
        "exec", plan.container_name,
        "timeout", str(PROBE_SECONDS),
        "bash", "-c", 'exec 3<>/dev/tcp/"$1"/"$2"', "_", str(entry["host"]), str(entry["port"]),
        check=False,
    ).code == 0


def probe_from_host(entry: dict[str, Any]) -> bool:
    """Does something answer on the host? `host.docker.internal` is the host itself, tried as 127.0.0.1.

    This only proves the host app is up. On Linux the Session reaches the host through the Docker
    bridge address, so an app bound to 127.0.0.1 only answers here and is still unreachable from the
    Session; the Session probe (a Session is running and the service is enabled) is the real answer.
    """
    host = "127.0.0.1" if entry["host"] == "host.docker.internal" else entry["host"]
    try:
        with socket.create_connection((host, entry["port"]), timeout=PROBE_SECONDS):
            return True
    except OSError:
        return False


def doctor_lines(docker: "Docker", plan: "SessionPlan") -> list[tuple[str, str]]:
    """(level, text) per configured Host Service for `asb doctor`. Levels: ok, warn, info.

    A Host Service that does not answer is a warning, not a failure: the host app is the user's to
    start (the CLI never starts host programs), so it does not make the setup broken.
    """
    from .runtime import DockerError  # local: runtime imports plan, which imports this module

    try:
        running = docker.is_running(plan.container_name)
        active = enabled(docker, plan)
    except DockerError:
        running, active = False, set()
    lines = []
    for e in plan.host_services:
        where = f"Host Service {e['name']} {e['host']}:{e['port']}"
        if running and e["name"] in active:
            if probe_from_session(docker, plan, e):
                lines.append(("ok", f"{where} answers from the Session"))
            else:
                lines.append(("warn", f"{where} does not answer from the Session; is the host app running and "
                                      "listening on an address the Docker host gateway reaches?"))
        elif running:
            lines.append(("info", f"{where} is not enabled in the Session; run `asb service enable {e['name']}`"))
        elif probe_from_host(e):
            lines.append(("ok", f"{where} answers on the host (not enabled: no Session is running)"))
        else:
            lines.append(("warn", f"{where} does not answer on the host; start the host app (no Session is running)"))
    return lines
