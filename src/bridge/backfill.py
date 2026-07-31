"""Import stray `HANDOFF.md` / `NEXT-SESSION.md` files as handoffs.

These files exist because the loop did not. Importing them populates the panel on
day one and exercises the capture path against messy real input rather than
fixtures.

Read-only with respect to the project repos: Bridge never writes into one, so a
backfilled file is left exactly where it is. `--dry-run` is the default, and the
handoff id is derived from the file's path and contents, so re-running imports
nothing new.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bridge.models import Handoff
from bridge.registry import resolve_project
from bridge.store import now_epoch

CANDIDATE_NAMES = ("HANDOFF.md", "NEXT-SESSION.md")

# A heading that plausibly introduces the prompt for the next session. Ordered
# by specificity: the first match wins.
PROMPT_HEADINGS = re.compile(
    r"^(#{1,4})\s*(next[- ]session(\s+prompt)?|prompt\s+for\s+(the\s+)?next"
    r"(\s+session)?|next\s+steps?|for\s+the\s+next\s+session|handoff\s+prompt)"
    r"\s*:?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Stable namespace so the same file always yields the same handoff id.
NAMESPACE = uuid.UUID("6f9b1e2c-0d4a-4d3f-9c1b-4a7e8f2d5b60")


@dataclass
class Candidate:
    path: Path
    project_path: str
    prompt: str
    structured: bool

    @property
    def id(self) -> str:
        digest = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()
        return str(uuid.uuid5(NAMESPACE, f"{self.path}\n{digest}"))

    @property
    def summary(self) -> str:
        if self.structured:
            return f"Backfilled from {self.path.name}"
        return f"Backfilled from {self.path.name} (unstructured: whole file)"


@dataclass
class BackfillStats:
    found: int = 0
    written: int = 0
    unstructured: int = 0
    dry_run: bool = True
    files: list[str] = field(default_factory=list)


def extract_prompt(text: str) -> tuple[str, bool]:
    """Return `(prompt, structured)`.

    A clearly delimited next-session section becomes the prompt; anything else
    keeps the whole file, because a handoff that loses the operator's own words
    is worse than one carrying too many.
    """
    match = PROMPT_HEADINGS.search(text)
    if match is None:
        return text.strip(), False

    level = len(match.group(1))
    body_start = match.end()
    # Stop at the next heading of the same or higher level.
    following = re.compile(rf"^#{{1,{level}}}\s+\S", re.MULTILINE)
    nxt = following.search(text, body_start)
    section = text[body_start:nxt.start()] if nxt else text[body_start:]
    section = section.strip()
    if not section:
        return text.strip(), False
    return section, True


def project_roots(store, cfg) -> list[Path]:
    """Indexed projects, plus the direct children of `~/dev`.

    `cfg.dev_dir` was previously unused; a stray handoff file is exactly the case
    where a repo that has no transcripts yet still has something to say.
    """
    roots = {Path(row["path"]) for row in store.projects()}
    if cfg.dev_dir.is_dir():
        roots.update(p for p in cfg.dev_dir.iterdir() if p.is_dir())
    return sorted(roots)


def discover(store, cfg) -> list[Candidate]:
    out: list[Candidate] = []
    for root in project_roots(store, cfg):
        for name in CANDIDATE_NAMES:
            path = root / name
            try:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # unreadable file is never fatal
            if not text.strip():
                continue
            prompt, structured = extract_prompt(text)
            out.append(Candidate(path=path, project_path=str(root),
                                 prompt=prompt, structured=structured))
    return out


def run(store, cfg, write: bool = False) -> BackfillStats:
    stats = BackfillStats(dry_run=not write)
    for candidate in discover(store, cfg):
        stats.found += 1
        stats.files.append(str(candidate.path))
        if not candidate.structured:
            stats.unstructured += 1
        if not write:
            continue
        handoff = Handoff(
            id=candidate.id,
            project_path=candidate.project_path,
            next_prompt=candidate.prompt,
            summary=candidate.summary,
            created_at=int(candidate.path.stat().st_mtime) or now_epoch(),
        )
        before = store.get_handoff(handoff.id)
        store.create_handoff(handoff, resolve_project(store, candidate.project_path))
        if before is None:
            stats.written += 1
    return stats
