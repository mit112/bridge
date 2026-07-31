"""Which transcript directories are real projects, and what to call them.

Transcript directory names path-encode `/` as `-`, which is lossy:
`-Users-mitsheth-dev-Job-apps` maps to both "Job apps" (the real directory)
and "Job-apps". Real paths therefore come only from the `cwd` field inside a
transcript. Nothing here decodes a directory name into a path.
"""

from pathlib import Path

# Container directories that hold projects but are not projects themselves.
# Matched EXACTLY, never by prefix: "-Users-mitsheth" is a prefix of every
# other entry, and an ancestor-based rule would wrongly hide real parents
# like -Users-mitsheth-dev-projectY (which contains boardwatch).
CONTAINER_DIRS = frozenset({
    "-Users-mitsheth",
    "-Users-mitsheth-dev",
    "-Users-mitsheth-Documents",
})

NOISE_PREFIXES = (
    "-private-tmp-",
    "-Users-mitsheth--claude",
    "-Users-mitsheth--local-share-ecc-homunculus",
    "-Volumes-",
)


def is_noise(dir_name: str) -> bool:
    return dir_name in CONTAINER_DIRS or dir_name.startswith(NOISE_PREFIXES)


def display_name(project_path: str) -> str:
    return Path(project_path.rstrip("/")).name


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
