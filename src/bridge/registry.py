"""Which transcript directories are real projects, and what to call them.

Transcript directory names path-encode `/`, `.` and space all as `-`, which is
lossy: `-Users-you-dev-Job-apps` maps to both "Job apps" (the real directory)
and "Job-apps". Real paths therefore come only from the `cwd` field inside a
transcript. Nothing here decodes a directory name into a path.

Every rule below is derived from the *running user's* home. Hardcoding one
username here is not a cosmetic leak: `is_noise` decides which directories
become project cards, so a foreign home means the panel shows the user's home
directory and their dotfile directories as projects, and filters nothing.
"""

import os
import re
from collections.abc import Iterable
from pathlib import Path

# Directory names directly under $HOME that hold projects but are not projects
# themselves. Matched EXACTLY, never by prefix: the encoded home is a prefix of
# every project under it, and an ancestor-based rule would wrongly hide real
# parents like `~/dev/projectY` (which contains `projectY/nested-app`).
CONTAINER_NAMES = ("dev", "Documents")

# Independent of $HOME: Claude Code's own sandbox transcripts, and mounted
# volumes, are never the user's projects.
GLOBAL_NOISE_PREFIXES = ("-private-tmp-", "-Volumes-")


def encode_path(path: Path | str) -> str:
    """Claude Code's transcript-directory encoding, reproduced exactly.

    `/`, `.` and space all collapse to `-`. Encoding forward is well defined;
    it is only *decoding* that is ambiguous, which is why nothing here does it.
    """
    return re.sub(r"[/. ]", "-", str(path))


def _rules(home: Path | None) -> tuple[frozenset[str], tuple[str, ...]]:
    """(container dirs, noise prefixes) for one home. Cheap enough to redo."""
    encoded = encode_path(Path(home) if home is not None else Path.home())
    containers = frozenset(
        {encoded} | {f"{encoded}-{name}" for name in CONTAINER_NAMES}
    )
    # `<home>--` is every dotdir under home: `.claude`, `.config`, `.local/...`.
    # Hidden directories are not projects, so the rule generalises rather than
    # naming the handful of tools that happen to be installed here.
    return containers, (*GLOBAL_NOISE_PREFIXES, f"{encoded}--")


def is_noise(dir_name: str, home: Path | None = None) -> bool:
    """`home` is injected only by tests; production always reads `Path.home()`."""
    containers, prefixes = _rules(home)
    return dir_name in containers or dir_name.startswith(prefixes)


def is_noise_path(path: Path | str, home: Path | None = None) -> bool:
    """`is_noise`, asked about a real directory instead of a transcript dir name.

    Skipping the `-Users-you` transcript directory is not enough to keep the
    home directory out of the project list, because a project row is created
    from the `cwd` recorded INSIDE a transcript, not from the directory the
    transcript sits in: a session that starts in a project and then `cd`s home
    records home as its cwd, and lands in a directory `is_noise` never sees.
    Encoding the path back into the same namespace answers the question with
    the one existing rule set rather than a second one that can drift from it.
    """
    return is_noise(encode_path(path), home)


def nesting_parent(path: Path | str, registered: Iterable[str]) -> str | None:
    """The registered project `path` is a sub-directory artefact of, or None.

    Running Claude in `boardwatch/2026-09-08-session` makes that scratch
    directory look exactly like a new project: it has its own transcript
    directory and its own cwd, and nothing in the transcript says it is not a
    project of its own. The containment does.

    The rule settled on: an ancestor that is a git worktree root, where `path`
    itself is not one. Comparison is per path component (`is_relative_to`), so
    two siblings -- `Job apps/northwind` and `Job apps/northgate` -- can never
    match each other however similar their names. Requiring the ancestor to be
    a worktree root stops a merely-registered container directory swallowing
    everything beneath it. Requiring the descendant NOT to be one keeps a
    submodule, or a repo independently cloned inside another repo, visible:
    git itself calls those separate worktrees, so Bridge does too.

    A monorepo *package* under a registered monorepo root is caught by this,
    knowingly: git reports it as part of the same worktree, so there is nothing
    left to tell it apart from a scratch directory. The caller only applies
    this to projects it is seeing for the first time and hides rather than
    deletes them, so the cost is one Restore click that then sticks.
    """
    p = Path(path)
    if (p / ".git").exists():
        return None
    for other in registered:
        parent = Path(other)
        if parent == p or not p.is_relative_to(parent):
            continue
        if (parent / ".git").exists():
            return other
    return None


def display_name(project_path: str) -> str:
    return Path(project_path.rstrip("/")).name


def resolve_project(store, raw_path: str) -> int:
    """Attach an arbitrary cwd to its canonical project, creating the row if new.

    A handoff can arrive from a project that has never been indexed, or from one
    of the old `~/Documents/...` locations, so this resolves through the same
    alias table indexing uses. Skipping that step would re-split the history
    path aliasing just merged.
    """
    canonical = store.alias_map().get(raw_path, raw_path)
    return store.upsert_project(canonical, display_name(canonical))


def transcript_files(projects_dir: Path) -> list[Path]:
    """Every transcript under `projects_dir`, one level deep, in path order.

    Sorted by `os.fspath` rather than by the `Path` objects themselves. On
    posix `PurePath.__lt__` compares `_str_normcase`, which *is* the path
    string, so this is the identical ordering -- but reaching it through the
    comparison protocol builds and caches that key one object at a time.
    Measured at 8 ms of a ~106 ms reindex on the real 9,200-file corpus,
    against a run that fires on every detected change.
    """
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(projects_dir.iterdir(), key=os.fspath):
        if not child.is_dir() or is_noise(child.name):
            continue
        out.extend(sorted(child.glob("*.jsonl"), key=os.fspath))
    return out
