"""The Hermes Kanban board of one Workspace (ADR-0010, ADR-0020).

Hermes keeps every board under one Kanban home, and one dispatcher sweeps all boards in it. The
Hermes Agent volume is shared by every Workspace (ADR-0006), so a shared home would let one
sandbox's dispatcher start workers for another Workspace's tasks. `HERMES_KANBAN_BOARD` alone is not
trusted to prevent that (docs/spikes/hermes-dashboard.md): each Workspace gets its own Kanban home,
a volume of its own, and the Session pins its board inside it. Isolation therefore holds even if the
board pin is ignored, because no other Workspace's board exists in that volume.

The board and its tasks persist across Sessions in that volume; `asb down` never removes it.
OMP and the other agents are not wired to Kanban: only Hermes reads these variables.
"""
from __future__ import annotations

import sys
from pathlib import Path

HOME = "/kanban"  # where the per-Workspace volume is mounted; HERMES_KANBAN_HOME points here
VOLUME_PREFIX = "agent-sandbox-kanban-"


def plan_entry(session: str, workspace: Path) -> dict:
    """Pure: the board slug, display name, volume and environment of this Workspace's Kanban."""
    slug = session[len("asb-"):]  # lowercase letters, digits and hyphens, unique per Workspace path
    return {
        "board": slug,
        "name": workspace.name,
        "volume": VOLUME_PREFIX + slug,
        "home": HOME,
        "env": {"HERMES_KANBAN_HOME": HOME, "HERMES_KANBAN_BOARD": slug},
        # `hermes kanban boards create` has `mkdir -p` semantics: an existing board is left as it is
        "create": ["hermes", "kanban", "boards", "create", slug, "--name", workspace.name],
    }


def ensure_board(docker, plan, err=None) -> bool:
    """Create the Workspace's board inside the running Session. Returns whether it worked.

    A failure costs only the board's display name and the pre-created board (Hermes falls back to the
    default board of this Workspace's own Kanban home), never isolation, so it is reported and the
    Session is kept.
    """
    err = sys.stderr if err is None else err
    k = plan.kanban
    if not k:
        return True
    r = docker.run("exec", plan.container_name, *k["create"], check=False)
    if r.code != 0:
        detail = (r.err or r.out).strip()
        print(f"warning: the Kanban board '{k['board']}' was not created" + (f" ({detail})" if detail else "")
              + "; Hermes Kanban still uses this Workspace's own volume, so other Workspaces' tasks stay out of reach",
              file=err)
        return False
    return True
