"""Generate Homebrew `resource` stanzas from uv.lock.

Single source of truth: the resolved graph in uv.lock. Run whenever the
resolved runtime graph changes and paste the output between the
GENERATED-RESOURCES markers in the tap's Formula/bridge.rb. CI diffs the
committed block against a fresh run of this script (see .github/workflows).
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Project runtime roots (from pyproject.toml [project].dependencies), with the
# extras we actually request. Dev extras (pytest, httpx2) are intentionally absent.
ROOTS: dict[str, tuple[str, ...]] = {
    "fastapi": (),
    "jinja2": (),
    "uvicorn": ("standard",),
}


def _load(lock_path: Path) -> dict[str, dict]:
    data = tomllib.loads(lock_path.read_text())
    return {p["name"]: p for p in data["package"]}


def _closure(pkgs: dict[str, dict]) -> set[str]:
    seen: set[str] = set()
    stack: list[tuple[str, tuple[str, ...]]] = list(ROOTS.items())
    while stack:
        name, extras = stack.pop()
        pkg = pkgs.get(name)
        if pkg is None or name in seen:
            continue
        seen.add(name)
        deps = list(pkg.get("dependencies", []))
        for extra in extras:
            deps += pkg.get("optional-dependencies", {}).get(extra, [])
        for dep in deps:
            stack.append((dep["name"], ()))
    return seen


def render_resources(lock_path: Path) -> str:
    pkgs = _load(lock_path)
    names = sorted(_closure(pkgs) - {"bridge"})
    blocks: list[str] = []
    for name in names:
        sdist = pkgs[name].get("sdist")
        if not sdist:
            raise SystemExit(f"{name}: no sdist in uv.lock (cannot make a resource)")
        url = sdist["url"]
        sha = sdist["hash"].removeprefix("sha256:")
        blocks.append(
            f'  resource "{name}" do\n'
            f'    url "{url}"\n'
            f'    sha256 "{sha}"\n'
            f"  end"
        )
    return "\n\n".join(blocks)


if __name__ == "__main__":
    print(render_resources(Path(sys.argv[1] if len(sys.argv) > 1 else "uv.lock")))
