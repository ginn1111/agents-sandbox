"""Gitignore-style glob matching for Secret, allow and Skip patterns.

Paths are POSIX paths relative to the Workspace. Matching is on names only and
never reads file contents (ADR-0004).

- ``**/`` matches zero or more directories; a trailing ``/**`` matches the
  directory itself and everything inside it.
- ``*`` and ``?`` never cross a ``/``; ``[abc]`` and ``[!abc]`` are classes.
- A pattern without an inner ``/`` matches that name at any depth; a leading
  or inner ``/`` anchors it to the Workspace root.
- A trailing ``/`` matches directories only.
- Matching is case-sensitive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Glob:
    pattern: str
    regex: re.Pattern
    dir_only: bool

    def matches(self, path: str, is_dir: bool = False) -> bool:
        if self.dir_only and not is_dir:
            return False
        return self.regex.fullmatch(path) is not None


def _segment(seg: str) -> str:
    out = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            while i + 1 < len(seg) and seg[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = i + 1
            if j < len(seg) and seg[j] in "!^":
                j += 1
            if j < len(seg) and seg[j] == "]":
                j += 1
            while j < len(seg) and seg[j] != "]":
                j += 1
            if j >= len(seg):  # unterminated class: literal bracket
                out.append(re.escape(c))
            else:
                body = seg[i + 1:j]
                neg = body[:1] in ("!", "^")
                if neg:
                    body = body[1:]
                out.append("[" + ("^" if neg else "") + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def compile_glob(pattern: str) -> Glob:
    original = pattern
    dir_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")
    segments = [s for s in pattern.split("/") if s]
    if not segments:
        raise ValueError(f"empty glob pattern: {original!r}")
    if not anchored and len(segments) == 1 and segments[0] != "**":
        segments.insert(0, "**")
    collapsed: list[str] = []
    for s in segments:
        if s == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(s)
    acc = ""
    last = len(collapsed) - 1
    for i, seg in enumerate(collapsed):
        if seg == "**":
            if i == last:
                acc = acc[:-1] + "(?:/.*)?" if acc else ".*"
            else:
                acc += "(?:.*/)?"
        else:
            acc += _segment(seg) + ("" if i == last else "/")
    return Glob(original, re.compile(acc, re.DOTALL), dir_only)
