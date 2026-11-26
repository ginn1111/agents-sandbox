"""Human-readable rendering of a SessionPlan (`asb check`)."""
from __future__ import annotations

from .plan import SessionPlan

# Project config rules that make masking weaker: they exempt or skip paths.
WEAKENING = {"secrets.allow", "secrets.skip"}


def render(p: SessionPlan) -> str:
    lines = [f"Workspace: {p.workspace}"]
    lines.append("Config:    " + (", ".join(p.config_files) if p.config_files else "defaults (no config files found)"))
    lines.append("")
    for e in p.entries:
        name = e.path + ("/" if e.kind == "dir" else "")
        lines.append(f"{e.status:<8} {name:<28} ({e.reason})")
    if not any(e.status == "MASKED" for e in p.entries):
        lines.append("No Secret files found; nothing will be masked.")
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
