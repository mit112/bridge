#!/usr/bin/env python3
"""Falsification harness: prove a test actually constrains the implementation.

Phase 1 shipped seven tests that passed while constraining nothing, with the
suite green the whole time. Green was never once informative. The only way to
learn whether a test guards a behaviour is to break the behaviour and watch the
test fail.

A mutation result is only meaningful if all of these hold, so this harness
enforces each one mechanically rather than trusting the operator to remember:

1. **The target file is committed.** `git checkout --` restores to HEAD, so
   mutating an uncommitted implementation *deletes* it at the first restore and
   every later result is measured against a missing feature.
2. **The control run passes.** If it does not, you are measuring a broken
   baseline. This is also the tell for the scratch-copy trap: a copy on
   `PYTHONPATH` does not override the venv-installed `bridge` package, so the
   control fails and nothing being measured is real.
3. **Bytecode caching is off.** A mutation that only *moves* code is byte-size
   identical to the original and `git checkout` restores it within the same
   second. CPython validates a `.pyc` by (source mtime, source size) at
   one-second granularity, so both match and the stale bytecode compiled from
   the *mutated* source keeps executing — surviving both a source read and
   `inspect.getsource`, which show the correct file while the wrong bytecode
   runs. This cost an hour during path aliasing.
4. **The mutation actually applied.** An `old` string that matches zero times
   silently tests nothing; one that matches many times may mutate more than
   intended. Both are hard errors.

Usage:

    tools/falsify.py --spec tools/mutations/task1.json
    tools/falsify.py --file src/bridge/store.py \
        --old 'status=?' --new 'status=?' \
        --test tests/test_store.py::test_supersede --repeat 5

Spec JSON is a list of mutation objects (or `{"mutations": [...]}`):

    [{"name": "drop the supersede UPDATE",
      "file": "src/bridge/store.py",
      "old": "...exact source text...",
      "new": "...replacement...",
      "tests": ["tests/test_store.py::test_supersede_marks_the_old_one"],
      "repeat": 1,
      "expect_count": 1}]

Exit status is 0 only if every mutation was CAUGHT.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GIT = "/usr/bin/git"
TAIL_LINES = 25


@dataclass
class Mutation:
    name: str
    file: str
    old: str
    new: str
    tests: list[str]
    repeat: int = 1
    expect_count: int = 1


@dataclass
class Result:
    mutation: Mutation
    caught: bool
    control_passed: bool
    failures: int
    runs: int
    detail: str = ""
    mutant_output: str = ""
    control_output: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.control_passed:
            return "CONTROL-FAILED"
        return "CAUGHT" if self.caught else "SURVIVED"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [GIT, "-C", str(REPO), *args], capture_output=True, text=True
    )


def _require_committed(rel: str) -> None:
    """Rule 1. `git checkout --` restores to HEAD; uncommitted work would be lost."""
    tracked = _git("ls-files", "--error-unmatch", rel)
    if tracked.returncode != 0:
        raise SystemExit(
            f"refusing to mutate {rel}: not tracked by git.\n"
            "Commit the implementation first — restore is `git checkout --`, "
            "which would delete an untracked file's contents entirely."
        )
    if _git("diff", "--quiet", "HEAD", "--", rel).returncode != 0:
        raise SystemExit(
            f"refusing to mutate {rel}: it has uncommitted changes.\n"
            "Commit the implementation BEFORE falsifying it. Otherwise the first\n"
            "restore reverts to HEAD, deleting the feature under test, and every\n"
            "later mutation is measured against a missing implementation."
        )


def _clear_pycache() -> int:
    """Rule 3. Stale bytecode from a byte-size-identical mutation outlives a restore."""
    n = 0
    for d in list(REPO.rglob("__pycache__")):
        if ".venv" in d.parts:
            continue
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


def _pytest(tests: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "",
        },
    )


def _tail(proc: subprocess.CompletedProcess) -> str:
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return "\n".join(lines[-TAIL_LINES:])


def run_mutation(m: Mutation) -> Result:
    path = REPO / m.file
    _require_committed(m.file)
    original = path.read_bytes()

    if m.old not in m.new and m.old == m.new:
        raise SystemExit(f"{m.name}: old and new are identical; nothing would change")

    text = original.decode()
    found = text.count(m.old)
    if found != m.expect_count:
        raise SystemExit(
            f"{m.name}: `old` matched {found} times in {m.file}, "
            f"expected {m.expect_count}. A zero-match mutation tests nothing."
        )

    notes = [f"cleared {_clear_pycache()} __pycache__ dirs"]

    # Control: the committed implementation must pass the tests we are about to
    # rely on. A failing control means the measurement is worthless (rule 2).
    control = _pytest(m.tests)
    if control.returncode != 0:
        return Result(
            mutation=m, caught=False, control_passed=False, failures=0,
            runs=0, detail="control run failed on unmutated code",
            control_output=_tail(control), notes=notes,
        )

    failures = 0
    mutant_output = ""
    try:
        path.write_text(text.replace(m.old, m.new))
        # Confirm the bytes on disk really changed before believing any result.
        if path.read_bytes() == original:
            raise SystemExit(f"{m.name}: file unchanged after write; aborting")
        for i in range(m.repeat):
            _clear_pycache()
            mutant = _pytest(m.tests)
            if mutant.returncode != 0:
                failures += 1
                if not mutant_output:
                    mutant_output = _tail(mutant)
            elif i == 0:
                mutant_output = _tail(mutant)
    finally:
        _git("checkout", "--", m.file)
        _clear_pycache()
        restored = path.read_bytes()
        if restored != original:
            notes.append("!! RESTORE MISMATCH — file does not match pre-mutation bytes")
        else:
            notes.append("restored and byte-identical to HEAD")

    return Result(
        mutation=m, caught=failures > 0, control_passed=True, failures=failures,
        runs=m.repeat, mutant_output=mutant_output,
        detail=f"{failures}/{m.repeat} mutant runs failed", notes=notes,
    )


def load_spec(p: Path) -> list[Mutation]:
    data = json.loads(p.read_text())
    if isinstance(data, dict):
        data = data["mutations"]
    return [Mutation(**d) for d in data]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="falsify")
    ap.add_argument("--spec", type=Path)
    ap.add_argument("--file")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--test", action="append", default=[])
    ap.add_argument("--name", default="ad-hoc mutation")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--expect-count", type=int, default=1)
    args = ap.parse_args(argv)

    if args.spec:
        mutations = load_spec(args.spec)
    elif args.file and args.old is not None and args.test:
        mutations = [
            Mutation(args.name, args.file, args.old, args.new or "",
                     args.test, args.repeat, args.expect_count)
        ]
    else:
        ap.error("need --spec, or --file/--old/--new/--test")

    results = [run_mutation(m) for m in mutations]

    print("\n" + "=" * 72)
    for r in results:
        print(f"\n### {r.verdict}: {r.mutation.name}")
        print(f"    file:  {r.mutation.file}")
        print(f"    tests: {' '.join(r.mutation.tests)}")
        print(f"    {r.detail}")
        for n in r.notes:
            print(f"    - {n}")
        body = r.mutant_output or r.control_output
        if body:
            label = "control output" if not r.control_passed else "observed output"
            print(f"\n    --- {label} ---")
            for line in body.splitlines():
                print(f"    {line}")
    caught = sum(1 for r in results if r.caught)
    print("\n" + "=" * 72)
    print(f"{caught}/{len(results)} mutations caught")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
