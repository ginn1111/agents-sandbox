"""Secret discovery: walk the Workspace and decide what is MASKED, ALLOWED or SKIPPED.

Names, types and inode numbers only: no file is ever opened or read, and nothing
is written (ADR-0004). Symlinks are never followed during the walk; a symlink with
a Secret name is resolved once and masked at its real path if that path is inside
the Workspace. Anything that cannot be scanned is reported as an error so the plan
fails closed instead of silently leaving a Secret file exposed.
"""
from __future__ import annotations

import os
import stat
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, Rule
from .globs import Glob, compile_glob

STATUS_ORDER = {"MASKED": 0, "ALLOWED": 1, "SKIPPED": 2}


@dataclass(frozen=True)
class Entry:
    status: str  # MASKED | ALLOWED | SKIPPED
    path: str  # POSIX path relative to the Workspace
    kind: str  # file | dir
    reason: str


@dataclass
class Discovery:
    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_SOURCE_LABEL = {"default": "", "global": " (global config)", "project": " (project config)", "cli": " (cli)"}


class _Rules:
    def __init__(self, rules: list[Rule]):
        self.items = [(compile_glob(r.pattern), r) for r in rules]

    def first(self, path: str, is_dir: bool) -> Rule | None:
        for g, r in self.items:
            if g.matches(path, is_dir):
                return r
        return None


def _label(kind: str, rule: Rule) -> str:
    return f"{kind} pattern {rule.pattern}{_SOURCE_LABEL.get(rule.source, '')}"


def _under(path: str, root: str) -> bool:
    return path.startswith(root + "/")


class _Scan:
    def __init__(self, workspace: Path, config: Config):
        self.ws = workspace
        self.ws_real = os.path.realpath(workspace)
        self.mask = _Rules(config.secret_mask)
        self.allow = _Rules(config.secret_allow)
        self.skip = _Rules(config.secret_skip)
        self.out = Discovery()
        self.masked: dict[str, tuple[str, str]] = {}  # rel -> (kind, reason)
        self.allowed: dict[str, tuple[str, str]] = {}
        self.skipped: dict[str, str] = {}
        self.inodes: dict[tuple[int, int], set[str]] = {}  # files with nlink > 1
        self.masked_keys: dict[tuple[int, int], str] = {}  # inode -> rel of a masked path
        self.pending: deque[tuple[str, str, os.stat_result | None]] = deque()
        self.scanned_roots: set[str] = set()
        self.git_files: list[str] = []

    # -- walking
    def _entries(self, abs_dir: str, rel: str, what: str):
        try:
            with os.scandir(abs_dir) as it:
                return sorted(it, key=lambda e: e.name)
        except OSError as e:
            self.out.errors.append(f"cannot scan {rel or '.'} ({what}): {e.strerror or e}")
            return []

    def _note_inode(self, entry: os.DirEntry, rel: str, masked_source: bool) -> None:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError as e:
            self.out.errors.append(f"cannot stat {rel}: {e.strerror or e}")
            return
        if st.st_nlink > 1:
            key = (st.st_dev, st.st_ino)
            self.inodes.setdefault(key, set()).add(rel)
            if masked_source:
                self._remember_key(key, rel)

    def _remember_key(self, key: tuple[int, int], rel: str) -> None:
        cur = self.masked_keys.get(key)
        if cur is None or rel < cur:
            self.masked_keys[key] = rel

    def walk_masked(self, abs_dir: str, rel: str) -> None:
        """Inside a masked directory: list nothing, but find hard links and symlinks."""
        if rel in self.scanned_roots:
            return
        self.scanned_roots.add(rel)
        try:
            for e in self._entries(abs_dir, rel, "masked directory, needed to find hard links"):
                child = f"{rel}/{e.name}"
                if e.is_symlink():
                    target = self._alias_target(e.path, child)
                    if target is not None:
                        self.pending.append((child, *target))
                elif e.is_dir(follow_symlinks=False):
                    if self.skip.first(child, True) is None:
                        self.walk_masked(e.path, child)
                else:
                    self._note_inode(e, child, masked_source=True)
        except RecursionError:
            self.out.errors.append(f"directory nesting too deep at {rel or '.'}; cannot scan completely")

    def walk(self, abs_dir: str, rel: str) -> None:
        try:
            for e in self._entries(abs_dir, rel, "Workspace directory"):
                child = f"{rel}/{e.name}" if rel else e.name
                if e.is_symlink():
                    self._symlink(e, child)
                elif e.is_dir(follow_symlinks=False):
                    self._dir(e, child)
                else:
                    self._file(e, child)
        except RecursionError:
            self.out.errors.append(f"directory nesting too deep at {rel or '.'}; cannot scan completely")

    def _dir(self, e: os.DirEntry, rel: str) -> None:
        skip = self.skip.first(rel, True)
        if skip:
            self.skipped[rel] = f"skip pattern {skip.pattern}{_SOURCE_LABEL.get(skip.source, '')}"
            return
        mask = self.mask.first(rel, True)
        if mask:
            allow = self.allow.first(rel, True)
            if allow:
                self.allowed[rel] = ("dir", _label("allow", allow))
            else:
                self.masked[rel] = ("dir", _label("", mask).strip())
                self.walk_masked(e.path, rel)
                return
        self.walk(e.path, rel)

    def _file(self, e: os.DirEntry, rel: str) -> None:
        mask = self.mask.first(rel, False)
        if mask:
            allow = self.allow.first(rel, False)
            if allow:
                self.allowed[rel] = ("file", _label("allow", allow))
            else:
                self.masked[rel] = ("file", _label("", mask).strip())
                self._note_inode(e, rel, masked_source=True)
                return
        elif e.name == ".git":
            self.git_files.append(rel)
        self._note_inode(e, rel, masked_source=False)

    def _inside(self, path: str) -> bool:
        return os.path.commonpath((self.ws_real, path)) == self.ws_real

    def _alias_target(self, path: str, rel: str) -> tuple[str, os.stat_result | None] | None:
        try:
            real = os.path.realpath(path, strict=True)
            if not self._inside(real):
                return real, None
            st = os.lstat(real)
            if stat.S_ISLNK(st.st_mode):
                self.out.errors.append(f"cannot resolve symlink {rel}: target changed during discovery")
                return None
            return real, st
        except (FileNotFoundError, NotADirectoryError):
            # A dangling alias inside the Workspace has no target to mask. Keep
            # the warning for dangling aliases that refer outside the Workspace.
            real = os.path.realpath(path)
            return (real, None) if not self._inside(real) else None
        except OSError as err:
            self.out.errors.append(f"cannot resolve symlink {rel}: {err.strerror or err}")
            return None

    def _symlink(self, e: os.DirEntry, rel: str) -> None:
        if self.mask.first(rel, True) is None:
            return  # No Secret pattern applies, even if the alias points to a directory.
        target = self._alias_target(e.path, rel)
        if target is None:
            return
        _, st = target
        is_dir = st is not None and stat.S_ISDIR(st.st_mode)
        if self.mask.first(rel, is_dir) is None:
            return
        allow = self.allow.first(rel, is_dir)
        if allow:
            self.allowed[rel] = ("dir" if is_dir else "file", _label("allow", allow))
        else:
            self.pending.append((rel, *target))

    # -- aliases
    def resolve_symlinks(self) -> None:
        done: set[str] = set()
        while self.pending:
            link_rel, real, st = self.pending.popleft()
            if real in done:
                continue
            if real == self.ws_real:
                self.out.errors.append(
                    f"symlink {link_rel} points at the Workspace root; cannot mask it without hiding the whole Workspace"
                )
                continue
            if st is None:
                self.out.warnings.append(
                    f"symlink {link_rel} points outside the Workspace; it is not followed and its target is not mounted"
                )
                continue
            done.add(real)
            rel = os.path.relpath(real, self.ws_real).replace(os.sep, "/")
            reason = f"target of symlink {link_rel}"
            if stat.S_ISDIR(st.st_mode):
                self.masked.setdefault(rel, ("dir", reason))
                self.walk_masked(real, rel)
            else:
                self.masked.setdefault(rel, ("file", reason))
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    self.inodes.setdefault(key, set()).add(rel)
                    self._remember_key(key, rel)

    def covered(self, rel: str) -> bool:
        return any(_under(rel, root) for root, (kind, _) in self.masked.items() if kind == "dir")

    def add_hard_links(self) -> None:
        for key, source in self.masked_keys.items():
            for rel in sorted(self.inodes.get(key, ())):
                if rel == source or rel in self.masked or self.covered(rel):
                    continue
                self.allowed.pop(rel, None)
                self.masked[rel] = ("file", f"hard link to {source}")

    def external_git(self) -> None:
        for rel in self.git_files:
            if rel in self.masked or self.covered(rel):
                continue
            path = self.ws / rel
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(st, opened):
                        continue
                    first = os.read(fd, 4096).decode(errors="replace").splitlines()[:1]
                finally:
                    os.close(fd)
                if not first or not first[0].startswith("gitdir:"):
                    continue
                pointer = first[0].split(":", 1)[1].strip()
                if not pointer or "\0" in pointer:
                    self.out.warnings.append(f"cannot inspect git pointer {rel}: invalid target")
                    continue
                target = Path(os.path.realpath(path.parent / pointer))
            except (OSError, ValueError, RecursionError) as err:
                self.out.warnings.append(f"cannot inspect git pointer {rel}: {err}")
                continue
            ws = Path(self.ws_real)
            if ws not in (target, *target.parents):
                self.out.warnings.append(
                    f"{rel} points to {target}, outside the Workspace; it is not mounted, "
                    "so git will not work inside the Session"
                )

    def finish(self) -> Discovery:
        entries: list[Entry] = []
        for rel, (kind, reason) in self.masked.items():
            if not self.covered(rel):
                entries.append(Entry("MASKED", rel, kind, reason))
        for table, status in ((self.allowed, "ALLOWED"),):
            for rel, (kind, reason) in table.items():
                if rel not in self.masked and not self.covered(rel):
                    entries.append(Entry(status, rel, kind, reason))
        for rel, reason in self.skipped.items():
            if not self.covered(rel):
                entries.append(Entry("SKIPPED", rel, "dir", reason))
        entries.sort(key=lambda x: (STATUS_ORDER[x.status], x.path))
        self.out.entries = entries
        self.out.warnings.sort()
        return self.out


def discover(workspace: Path, config: Config) -> Discovery:
    scan = _Scan(Path(workspace), config)
    scan.walk(str(workspace), "")
    scan.resolve_symlinks()
    scan.add_hard_links()
    scan.external_git()
    return scan.finish()
