"""Human-readable rendering of a SessionPlan (`asb check`)."""
from __future__ import annotations

from . import host_browser
from .plan import SessionPlan

# Project config rules that make masking weaker: they exempt or skip paths.
WEAKENING = {"secrets.allow", "secrets.skip"}


def render(p: SessionPlan) -> str:
    lines = [f"Workspace: {p.workspace}"]
    lines.append("Config:    " + (", ".join(p.config_files) if p.config_files else "defaults (no config files found)"))
    lines.append("")
    if p.netguard_name:
        lines.append(f"Network:   Netguard {p.netguard_name}: host, LAN and private ranges blocked, public internet open, IPv6 off")
    else:
        lines.append("Network:   offline: no network at all")
    for e in p.entries:
        name = e.path + ("/" if e.kind == "dir" else "")
        lines.append(f"{e.status:<8} {name:<28} ({e.reason})")
    if not any(e.status == "MASKED" for e in p.entries):
        lines.append("No Secret files found; nothing will be masked.")
    lines.append("")
    if p.host_services:
        lines.append("Host Services: (all off until enabled; every other host port stays blocked)")
        for h in p.host_services:
            lines.append(f"  {h['name']} {h['host']}:{h['port']}  (enable: asb service enable {h['name']})")
    else:
        lines.append("Host Services:  none")
    lines += host_browser.render_lines(p)
    if p.agents:
        lines.append("Agents:")
        for a in p.agents:
            lines.append(f"  {a['name']:<8} {' '.join(a['command'])}  (home {a['home']}, volume {a['volume']})")
        lines.append("Volumes:        " + ", ".join(p.volumes) + " (kept by `asb down`; `docker volume rm` to log out)")
        lines.append("")
    if p.kanban:
        lines.append(f"Kanban:   Hermes board '{p.kanban['board']}' in this Workspace's own volume {p.kanban['volume']} "
                     f"(kept by `asb down`; only Hermes uses it)")
    if p.portals:
        lines.append("Portals:  (none started by `up`; `asb portal start <name>` publishes one on 127.0.0.1 only)")
        for e in p.portals:
            how = "asb runs: " + " ".join(e["command"]) if e["command"] else "the agent starts it"
            lines.append(f"  {e['name']:<12} port {e['port']} → 127.0.0.1:<random>{e['open']}  ({how})")
        lines.append("")
    for key, value in p.project_rules:
        note = " (weakens masking)" if key in WEAKENING else ""
        lines.append(f"Project config: {key} {value}{note}")
    for w in p.warnings:
        lines.append(f"Warning: {w}")
    for err in p.errors:
        lines.append(f"Error: {err}")
    lines.append("")
    lines.append("Limits:")
    lines += [f"  - {l}" for l in p.limits]
    lines.append(f"Result: {p.result}")
    return "\n".join(lines)
