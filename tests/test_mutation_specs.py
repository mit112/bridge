"""The falsifier's own blind spot: an anchor that has quietly stopped matching.

`tools/falsify.py` treats a mismatched `old` as a hard error, so a drifted
anchor is never silently mutated. But it only learns that at the moment it
tries to apply one, and it stops the whole spec there. A spec whose third
mutation no longer matches therefore reports nothing at all about its fourth,
and the only way to notice is to run every spec and read every line.

That is how seven anchors across four specs came to test nothing for two
phases: `_atomic_write` grew an argument, `<pre>` became a `<textarea>`, PATCH
hoisted a lookup into a local, and `bridge launch` added a second empty-prompt
check that made the `bridge handoff` anchor ambiguous. Each was invisible
because falsify aborted before reaching it, and repairing one only exposed the
next. One of them was masking a genuine SURVIVED.

This module is the cheap standing check that makes that class of drift fail on
the ordinary suite, in one run, with every offender named at once. It asserts
only that each anchor still *matches* — whether the test it names actually
constrains anything is what falsify measures, and it cannot be answered here.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC_DIR = REPO / "tools" / "mutations"
SPECS = sorted(SPEC_DIR.glob("*.json"))


def _mutations(spec: Path):
    data = json.loads(spec.read_text(encoding="utf-8"))
    return data["mutations"] if isinstance(data, dict) else data


def test_there_are_specs_to_check():
    """A glob that silently matched nothing would make every test below vacuous."""
    assert SPECS, f"no mutation specs under {SPEC_DIR}"


def test_every_mutation_anchor_matches_its_file_exactly_as_often_as_declared():
    """Reported in full rather than at the first failure.

    Anchors drift in batches -- one refactor invalidates every anchor into the
    function it touched -- and falsify already gives the stop-at-the-first
    behaviour. Bisecting the same list one commit-and-rerun at a time is the
    slow path this exists to avoid.
    """
    problems = []
    for spec in SPECS:
        for m in _mutations(spec):
            target = REPO / m["file"]
            if not target.is_file():
                problems.append(f"{spec.name}: {m['name']!r} -> no such file {m['file']}")
                continue
            want = m.get("expect_count", 1)
            got = target.read_text(encoding="utf-8").count(m["old"])
            if got != want:
                problems.append(
                    f"{spec.name}: {m['name']!r} -> `old` matches {got}x in "
                    f"{m['file']}, expected {want}x"
                )
    assert not problems, "drifted mutation anchors:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("spec", SPECS, ids=lambda p: p.stem)
def test_every_mutation_names_a_test_that_exists(spec):
    """A mutation whose test was renamed away reports CAUGHT for the wrong reason.

    pytest exits non-zero on an unrecognised node id, and falsify reads a
    non-zero mutant run as caught -- so a deleted test looks exactly like a
    test doing its job. Only the file and function name are checked here;
    resolving parametrised ids is pytest's job, not this test's.
    """
    missing = []
    for m in _mutations(spec):
        for node in m["tests"]:
            path, _, func = node.partition("::")
            if not (REPO / path).is_file():
                missing.append(f"{m['name']!r} -> no such test file {path}")
            elif func and f"def {func.split('[')[0]}(" not in (REPO / path).read_text(
                encoding="utf-8"
            ):
                missing.append(f"{m['name']!r} -> {path} has no {func}")
    assert not missing, f"{spec.name} names tests that do not exist:\n  " + "\n  ".join(missing)
