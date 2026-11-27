"""Human-readable rendering of a SessionPlan (`asb check`)."""
from __future__ import annotations

from .plan import SessionPlan


def render(p: SessionPlan) -> str:
    lines = [f"Workspace: {p.workspace}"]
    lines.append("Config:    " + (", ".join(p.config_files) if p.config_files else "defaults (no config files found)"))
    lines.append("")
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
