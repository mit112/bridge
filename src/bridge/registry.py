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

import re
from pathlib import Path

# Directory names directly under $HOME that hold projects but are not projects
# themselves. Matched EXACTLY, never by prefix: the encoded home is a prefix of
# every project under it, and an ancestor-based rule would wrongly hide real
# parents like `~/dev/projectY` (which contains `projectY/boardwatch`).
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
    projects_dir = Path(projects_dir)
    if not projects_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir() or is_noise(child.name):
            continue
        out.extend(sorted(child.glob("*.jsonl")))
    return out
