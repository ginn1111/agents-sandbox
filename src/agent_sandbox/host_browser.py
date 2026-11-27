"""The Host Browser (ADR-0008, ADR-0013, ADR-0021): a Chrome the human starts on the host, driven from
the Session by ``chrome-devtools-mcp``.

Configuration (global config or ``--host-browser``) only makes it *available*; ``asb up`` opens
nothing. ``asb browser enable`` opens one TCP path, host ``host.docker.internal`` port 9222, through
the same Netguard helper Host Services use (ADR-0016), under the reserved service name
``host-browser``. The CLI never starts Chrome, never relays to it and never opens a browser: it
prints the command the human runs. Pure functions here, plus the helper calls and the host/Session
probes ``doctor`` needs.
"""
from __future__ import annotations

import ipaddress
import os
import shlex
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from . import host_services

if TYPE_CHECKING:  # pragma: no cover
    from .plan import SessionPlan
    from .runtime import Docker

SERVICE = "host-browser"  # name of the Netguard rule set; a Host Service may not use it
HOST = "host.docker.internal"
PORT = 9222
# Installed in the agent image (images/agent/asb-chrome-mcp). It connects chrome-devtools-mcp by IP
# address, because Chrome refuses DevTools requests whose Host header is a name other than localhost.
MCP_COMMAND = "asb-chrome-mcp"
WARNING = "agent controls host Chrome: intranet and file:/// reachable"
PROBE_SECONDS = 3
ENABLE_HINT = (
    "set network.host_browser: true in your global config (project config cannot) "
    "or pass --host-browser, then run `asb browser enable`"
)


class BrowserError(RuntimeError):
    """The Host Browser could not be enabled or disabled."""


def add_flag(parser) -> None:
    parser.add_argument("--host-browser", action="store_true",
                        help="make the Host Browser available for this command (global config or this flag only)")


def cli_options(args) -> dict[str, Any]:
    return {"network": {"host_browser": True}} if getattr(args, "host_browser", False) else {}


def reserved_error(name: str, host: str, port: int) -> str | None:
    """Why a configured Host Service may not claim the Host Browser's name or address, or None.

    Otherwise project config could open Chrome's debugging port as a plain Host Service, and
    `asb service enable` would skip the opt-in and the warning the Host Browser needs.
    """
    if name == SERVICE:
        return f"'{SERVICE}' is reserved for the Host Browser (ADR-0008)"
    if host.lower() == HOST and port == PORT:
        return f"{HOST}:{PORT} is the Host Browser's debugging port; enable the Host Browser instead (ADR-0008)"
    return None


# --------------------------------------------------------------------------- host side: the profile

def profile_dir() -> Path:
    """The dedicated Chrome profile. Under the state directory, never inside a Workspace."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "agent-sandbox" / "chrome-profile"


def _inside(path: Path, workspace: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == workspace or workspace in real.parents


def launch_command(platform: str | None = None) -> str:
    """The exact command the human runs. A dedicated --user-data-dir: no cookies or logins of theirs.

    Recent Chrome ignores --remote-debugging-port on the default profile, so the dedicated profile
    is also what makes the port work. Nothing here opens the port to the network: Chrome binds it
    to 127.0.0.1 (see ``relay_command`` for Linux).
    """
    platform = sys.platform if platform is None else platform
    binary = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
              if platform == "darwin" else "google-chrome")
    return " ".join([
        shlex.quote(binary),
        f"--remote-debugging-port={PORT}",
        shlex.quote(f"--user-data-dir={profile_dir()}"),
        "--no-first-run",
    ])


def relay_command(gateway: str | None) -> str | None:
    """Linux only: a relay from the Docker gateway address to Chrome's loopback port.

    Containers cannot reach the host's 127.0.0.1 on Linux. The relay listens on the gateway address
    only (never 0.0.0.0, which would put Chrome's debugging port on the LAN). None if ``gateway`` is
    not a plain, usable IPv4 address.
    """
    if not gateway or not host_services._IPV4.match(gateway):
        return None
    try:
        ip = ipaddress.IPv4Address(gateway)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_reserved or ip.is_link_local:
        return None
    return f"socat TCP-LISTEN:{PORT},bind={ip},fork,reuseaddr TCP:127.0.0.1:{PORT}"


# --------------------------------------------------------------------------- plan

def plan_entry(requested: bool, netguard: str | None, workspace: Path) -> tuple[dict | None, list[str], list[str]]:
    """(entry, warnings, errors) for the SessionPlan. The entry is None when the Host Browser is off.

    Nothing in it is active after `up`; `enable` is the argv `asb browser enable` runs in Netguard.
    """
    if not requested:
        return None, [], []
    if netguard is None:
        return None, ["offline: the Session has no network, so the Host Browser cannot be reached"], []
    errors = []
    if _inside(profile_dir(), workspace):
        errors.append(
            f"the Host Browser profile directory ({profile_dir()}) is inside the Workspace, where the agent "
            "could read its cookies; set XDG_STATE_HOME elsewhere")
    entry = {
        "available": True, "active": False, "service": SERVICE, "host": HOST, "port": PORT,
        "mcp": MCP_COMMAND,
        "enable": host_services.helper_argv(netguard, "enable", SERVICE, HOST, str(PORT)),
        "disable": host_services.helper_argv(netguard, "disable", SERVICE),
        "launch_command": launch_command(),
        "profile_dir": str(profile_dir()),
    }
    warning = f"Host Browser available (`asb browser enable`): {WARNING}"
    return entry, [warning], errors


def render_lines(p: "SessionPlan") -> list[str]:
    e = p.host_browser
    if not e:
        return ["Host Browser:   off"]
    return [f"Host Browser:   available, off until `asb browser enable` ({e['host']}:{e['port']}); "
            "you start Chrome yourself, `asb doctor` prints the command"]


def print_available(p: "SessionPlan", out: TextIO) -> None:
    """What `up` and `attach` say on every Session where the Host Browser is available."""
    if p.host_browser:
        print(f"warning: Host Browser available, not enabled: {WARNING}; "
              "run `asb browser enable` to open it", file=out)


# --------------------------------------------------------------------------- enable / disable

def enable(docker: "Docker", plan: "SessionPlan") -> dict[str, Any]:
    """Open the debugging port path in the running Session's Netguard. Nothing else changes."""
    if plan.netguard_name is None:
        raise BrowserError("this Session is offline: there is no network, so the Host Browser cannot be reached")
    if plan.host_browser is None:
        raise BrowserError(f"the Host Browser is not available: {ENABLE_HINT}")
    if plan.errors:
        raise BrowserError("; ".join(plan.errors))
    try:
        host_services._running_netguard(docker, plan)
    except host_services.ServiceError as e:
        raise BrowserError(str(e)) from e
    docker.run(*plan.host_browser["enable"])  # fails closed: a failed helper opens nothing and raises
    return plan.host_browser


def disable(docker: "Docker", session: str) -> None:
    """Close it. Needs no config, so it always works for a Session that is running."""
    netguard = f"{session}-netguard"
    if not docker.is_running(session):
        raise BrowserError("no Session is running for this Workspace; run `asb up` first")
    if not docker.container_running(netguard):
        raise BrowserError(f"Netguard {netguard} is not running; the Session has no network already")
    docker.run(*host_services.helper_argv(netguard, "disable", SERVICE))


def enable_lines(entry: dict[str, Any]) -> list[str]:
    return [
        f"Host Browser enabled: {entry['host']}:{entry['port']} is reachable from the Session "
        "(until `asb browser disable` or `asb down`)",
        f"Warning: {WARNING}",
        "asb never starts Chrome: start it yourself with the command `asb doctor` prints (dedicated profile).",
        f"Agents connect with `{entry['mcp']}` (chrome-devtools-mcp by IP address), "
        f"for example `claude mcp add chrome-devtools -- {entry['mcp']}`.",
    ]


# --------------------------------------------------------------------------- probes and doctor

def probe_host() -> bool:
    """Does something listen on 127.0.0.1:9222 on the host? Proves only that Chrome is up."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=PROBE_SECONDS):
            return True
    except OSError:
        return False


def probe_session(docker: "Docker", plan: "SessionPlan") -> tuple[bool, str]:
    """Ask Chrome for its version from inside the Session, exactly the way the MCP wrapper connects."""
    r = docker.run("exec", plan.container_name, MCP_COMMAND, "--probe", check=False)
    text = (r.out or r.err).strip()
    return r.code == 0, text.splitlines()[-1] if text else "no answer"


def bridge_gateway(docker: "Docker") -> str | None:
    """The default Docker bridge's gateway address, where `host-gateway` points on Linux."""
    from .runtime import DockerError  # local: runtime imports plan, which imports this module
    try:
        r = docker.run("network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}", check=False)
    except DockerError:
        return None
    out = r.out.strip()
    return out if r.code == 0 and out else None


def doctor_lines(docker: "Docker", plan: "SessionPlan | None") -> list[tuple[str, str]]:
    """(level, text) lines for `asb doctor`. Levels: ok, warn, info, and "" for a continuation line.

    `plan` is None when there is no usable Workspace or config; the launch command is printed anyway.
    """
    from .runtime import DockerError  # local: see bridge_gateway

    lines: list[tuple[str, str]] = [
        ("info", "Host Browser: start Chrome yourself with this command (asb never starts Chrome; "
                 "dedicated profile, none of your own cookies or logins):"),
        ("", "  " + launch_command()),
    ]
    if sys.platform.startswith("linux"):
        relay = relay_command(bridge_gateway(docker))
        lines.append(("info", "On Linux Chrome listens on loopback (127.0.0.1) only, which the Session cannot reach; "
                              "run a relay bound to the Docker gateway address (not 0.0.0.0):"))
        lines.append(("", "  " + (relay or f"socat TCP-LISTEN:{PORT},bind=<docker gateway ip>,fork,reuseaddr "
                                           f"TCP:127.0.0.1:{PORT}   (see `docker network inspect bridge`)")))
    lines.append(("info", "Enabling it lets the agent reach the intranet and file:/// on this host through Chrome."))
    if probe_host():
        lines.append(("ok", f"Chrome answers on 127.0.0.1:{PORT} on the host"))
    else:
        lines.append(("info", f"no Chrome answers on 127.0.0.1:{PORT} on the host; start it with the command above"))
    if plan is None:
        return lines
    if plan.host_browser is None:
        if plan.netguard_name is None:
            lines.append(("info", "Host Browser is off: this Session is offline"))
        else:
            lines.append(("info", f"Host Browser is off: {ENABLE_HINT}"))
        return lines
    try:
        running = docker.is_running(plan.container_name)
        active = SERVICE in host_services.enabled(docker, plan)
    except DockerError:
        running, active = False, False
    if running and active:
        ok, detail = probe_session(docker, plan)
        if ok:
            lines.append(("ok", f"Host Browser enabled; Chrome answers from the Session ({detail})"))
        else:
            lines.append(("warn", f"Host Browser enabled but Chrome does not answer from the Session ({detail}); "
                                  "is Chrome running, and is something forwarding the Docker gateway address to "
                                  "Chrome's 127.0.0.1 port? Chrome alone does not listen there"))
    elif running:
        lines.append(("info", "Host Browser is available, not enabled in the Session; run `asb browser enable`"))
    else:
        lines.append(("info", "Host Browser is available; no Session is running (`asb up`, then `asb browser enable`)"))
    return lines
