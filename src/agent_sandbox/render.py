"""Human-readable rendering of a SessionPlan (`asb check`)."""
from __future__ import annotations

from .plan import SessionPlan


def render(p: SessionPlan) -> str:
    lines = [f"Workspace: {p.workspace}"]
    lines.append("Config:    " + (", ".join(p.config_files) if p.config_files else "defaults (no config files found)"))
    lines.append("")
    if p.netguard_name:
        lines.append(f"Network:   Netguard {p.netguard_name}: host, LAN and private ranges blocked, public internet open, IPv6 off")
    else:
        lines.append("Network:   offline: no network at all")
    if p.project_rules:
        for key, value in p.project_rules:
            lines.append(f"Project config: {key} {value}")
    for w in p.warnings:
        lines.append(f"Warning: {w}")
    lines.append("")
    lines.append("Limits:")
    lines += [f"  - {l}" for l in p.limits]
    lines.append(f"Result: {p.result}")
    return "\n".join(lines)
