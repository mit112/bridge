# Homebrew Tap (HEAD-tracking) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `mit112/homebrew-bridge` tap whose `Formula/bridge.rb` installs Bridge from `main` HEAD via a Python virtualenv, with every dependency resource generated from `uv.lock` (never hand-pinned), a HEAD-upgrade + audit + test CI lifecycle, and a README section in the main repo alongside the `uv` quick-start.

**Architecture:** This is Section D of the approved share/auto-update/Homebrew design. A HEAD-only Homebrew formula depends on `python@3.13`, tracks `main`, and installs with `virtualenv_install_with_resources`. A generator script in the **main** repo (`tools/brew_resources.py`) reads `uv.lock`, computes the runtime dependency closure (fastapi + uvicorn[standard] + jinja2, transitively; dev deps excluded), and emits `resource` stanzas straight from the sdist `url` + `sha256` already recorded in the lockfile — so the tap never becomes a second, drift-prone manifest. The main repo's CI fails if the committed resource block drifts from a fresh regeneration; the tap repo's CI exercises `brew install --HEAD`, a repeat `brew upgrade --fetch-HEAD`, `brew test`, and `brew audit --strict` on `macos-latest`. `brew upgrade` does **not** advance a HEAD formula on its own — the real update path is Bridge's own `bridge update` → `brew upgrade --fetch-HEAD mit112/bridge/bridge` (Section B engine, out of scope here).

**Tech Stack:** Homebrew (Ruby formula DSL, `Language::Python::Virtualenv`), Python 3.13, `hatchling` build backend, `uv`/`uv.lock`, GitHub Actions on `macos-latest`.

## Global Constraints

- macOS only. No Linux/Windows support (verbatim: "**macOS only.**"). CI runs on `macos-latest` only.
- Python floor: `requires-python = ">=3.13"`; the formula pins `depends_on "python@3.13"`.
- Runtime dependencies are exactly `fastapi`, `uvicorn[standard]`, `jinja2` (from `pyproject.toml`); `httpx2` and `pytest` are dev-only and MUST NOT appear as formula resources.
- Versioning tracks `main` HEAD; **no per-push tags**, so the formula is **HEAD-only** (`head "…", branch: "main"`; no stable `url`/`sha256`).
- Resources are **generated from `uv.lock` (the same locked graph), not hand-pinned.** Regenerate whenever the **resolved** graph changes; every `uvicorn[standard]` transitive dep gets a pinned resource (`url` + `sha256`).
- Install UX (verbatim): `brew tap mit112/bridge && brew install --HEAD bridge`.
- Plain `brew upgrade` will **not** advance a HEAD formula; the update path is Bridge's nudge → `bridge update` → `brew upgrade --fetch-HEAD`.
- Homebrew rollback is **UNSUPPORTED**; the documented fallback is `uv tool install`.
- Install layout MUST let the app's `install_method()` detect Homebrew by resolving the `bridge` executable under a Cellar of **both** `/opt/homebrew` and `/usr/local`. Standard `virtualenv_install_with_resources` layout (keg `libexec/bin/bridge`, symlinked into `<prefix>/bin`) satisfies this.
- The formula only needs to install a working `bridge` console entry point into `libexec/bin` and link it; the running panel's `bridge update` engine (Section B) re-bootstraps its LaunchAgent plist from the new keg path after upgrade — not the formula's job.
- Steps marked **ATTENDED — Mit runs this** require Mit's GitHub auth (creating/pushing the tap repo, enabling repo CI). Do not attempt them from an agent session.

---

### Task 1: Create and clone the `mit112/homebrew-bridge` tap repo

**Files:**
- Create (remote): GitHub repo `mit112/homebrew-bridge` (public)
- Create (local clone): `~/dev/homebrew-bridge/` with `Formula/` directory
- Create: `~/dev/homebrew-bridge/.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: an empty tap repo with a `Formula/` directory that Homebrew recognizes. A tap repo named `homebrew-<name>` is auto-mapped by Homebrew to the tap `mit112/bridge` (the `homebrew-` prefix is stripped), so `brew tap mit112/bridge` and formula ref `mit112/bridge/bridge` resolve to `Formula/bridge.rb` in this repo.

- [ ] **Step 1: ATTENDED — Mit runs this — create the public tap repo**

Homebrew requires the repo name to be `homebrew-bridge` (the `homebrew-` prefix is what makes `brew tap mit112/bridge` find it).

```bash
gh repo create mit112/homebrew-bridge --public \
  --description "Homebrew tap for Bridge (HEAD-tracking)"
```

Expected: `✓ Created repository mit112/homebrew-bridge on GitHub`.

- [ ] **Step 2: ATTENDED — Mit runs this — clone it locally**

```bash
git clone https://github.com/mit112/homebrew-bridge.git ~/dev/homebrew-bridge
mkdir -p ~/dev/homebrew-bridge/Formula
```

Expected: clone succeeds; `~/dev/homebrew-bridge/Formula/` exists (empty).

- [ ] **Step 3: Add a `.gitignore`**

Create `~/dev/homebrew-bridge/.gitignore`:

```gitignore
.DS_Store
*.bottle.tar.gz
```

- [ ] **Step 4: Verify Homebrew can see the (empty) tap directory**

Symlink the local checkout in as a tap so later tasks install from local edits without pushing:

```bash
brew tap-new mit112/bridge --no-git 2>/dev/null || true
TAP_DIR="$(brew --repository)/Library/Taps/mit112/homebrew-bridge"
rm -rf "$TAP_DIR"
ln -s ~/dev/homebrew-bridge "$TAP_DIR"
brew tap
```

Expected: `mit112/bridge` appears in `brew tap` output. (The symlink lets `brew install mit112/bridge/bridge` read your uncommitted local formula.)

- [ ] **Step 5: Commit the skeleton**

```bash
cd ~/dev/homebrew-bridge
git add .gitignore Formula/.keep 2>/dev/null; touch Formula/.keep; git add .gitignore Formula/.keep
git commit -m "Add tap skeleton"
```

---

### Task 2: Write the resource generator in the main repo

**Files:**
- Create: `/Users/mitsheth/dev/bridge/tools/brew_resources.py`
- Test: `/Users/mitsheth/dev/bridge/tests/test_brew_resources.py`

**Interfaces:**
- Consumes: `/Users/mitsheth/dev/bridge/uv.lock` (TOML; each `[[package]]` has `name`, `version`, `dependencies`, `optional-dependencies`, and an `sdist` table with `url` + `hash = "sha256:…"`).
- Produces: a function `render_resources(lock_path: Path) -> str` returning the Homebrew `resource "<name>" do … end` stanzas (one per runtime dependency, sorted by name, each with `url` and `sha256`), joined by blank lines. Also a `__main__` guard printing that string to stdout so it can be piped into the formula. The runtime closure is seeded from the project's non-dev requires (`fastapi`, `jinja2`, `uvicorn` **with its `standard` extra**) and walked transitively over each package's `dependencies`; `bridge` itself and the dev-only closure (`pytest`, `httpx2`, and anything reachable only through them) are excluded.

- [ ] **Step 1: Write the failing test**

Create `/Users/mitsheth/dev/bridge/tests/test_brew_resources.py`:

```python
from pathlib import Path

from tools.brew_resources import render_resources

LOCK = Path(__file__).resolve().parent.parent / "uv.lock"


def test_includes_runtime_and_standard_extra_deps():
    out = render_resources(LOCK)
    # Direct runtime deps
    assert 'resource "fastapi" do' in out
    assert 'resource "jinja2" do' in out
    assert 'resource "uvicorn" do' in out
    # uvicorn[standard] transitives must be present (the whole point)
    for pkg in ("httptools", "python-dotenv", "pyyaml", "uvloop", "watchfiles", "websockets"):
        assert f'resource "{pkg}" do' in out, f"missing standard-extra resource {pkg}"
    # Deeper transitives
    for pkg in ("pydantic", "pydantic-core", "starlette", "anyio", "markupsafe", "click", "h11"):
        assert f'resource "{pkg}" do' in out, f"missing transitive resource {pkg}"


def test_excludes_dev_only_deps():
    out = render_resources(LOCK)
    for pkg in ("pytest", "httpx2", "httpcore2", "iniconfig", "pluggy"):
        assert f'resource "{pkg}" do' not in out, f"dev-only {pkg} leaked into resources"
    assert 'resource "bridge" do' not in out


def test_each_resource_has_pypi_url_and_sha256():
    out = render_resources(LOCK)
    blocks = out.count(" do")
    assert out.count("url \"https://files.pythonhosted.org/") == blocks
    assert out.count("sha256 \"") == blocks
    # sha256 is a bare 64-hex, not the lockfile's "sha256:" prefix
    assert "sha256 \"sha256:" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/mitsheth/dev/bridge && uv run pytest tests/test_brew_resources.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tools.brew_resources'`.

- [ ] **Step 3: Write the generator**

Create `/Users/mitsheth/dev/bridge/tools/brew_resources.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/mitsheth/dev/bridge && uv run pytest tests/test_brew_resources.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite (throughput doctrine — verify before handing back)**

Run: `cd /Users/mitsheth/dev/bridge && uv run pytest -q`
Expected: all pass (existing count + 3 new).

- [ ] **Step 6: Commit**

```bash
cd /Users/mitsheth/dev/bridge
git add tools/brew_resources.py tests/test_brew_resources.py
git commit -m "Add uv.lock -> Homebrew resource generator"
```

---

### Task 3: Write the base formula (HEAD-only, no resources yet)

**Files:**
- Create: `~/dev/homebrew-bridge/Formula/bridge.rb`

**Interfaces:**
- Consumes: the tap directory from Task 1.
- Produces: a syntactically valid, HEAD-only formula named `bridge` with `depends_on "python@3.13"`, `head "https://github.com/mit112/bridge.git", branch: "main"`, an `install` calling `virtualenv_install_with_resources`, a `test` running `bridge --version`, and an empty, clearly delimited `# BEGIN GENERATED RESOURCES` / `# END GENERATED RESOURCES` block that Task 4 fills. The class name `Bridge` is derived from the filename `bridge.rb` (Homebrew convention).

- [ ] **Step 1: Write the formula**

Create `~/dev/homebrew-bridge/Formula/bridge.rb`:

```ruby
class Bridge < Formula
  include Language::Python::Virtualenv

  desc "Local control panel for Claude Code projects"
  homepage "https://github.com/mit112/bridge"
  # HEAD-only: Bridge tracks main HEAD with no per-push tags. There is no
  # stable url/sha256 on purpose. Install with `brew install --HEAD bridge`.
  head "https://github.com/mit112/bridge.git", branch: "main"
  license "MIT"

  depends_on "python@3.13"

  # BEGIN GENERATED RESOURCES -- do not edit by hand.
  # Regenerate from the main repo with:
  #   uv run python tools/brew_resources.py uv.lock
  # and paste the output between these markers. CI enforces no drift.
  # END GENERATED RESOURCES

  def install
    # Installs Bridge + all resources above into libexec as a venv and links
    # the `bridge` console entry point (pyproject [project.scripts]) into
    # #{bin}. The keg lives under <prefix>/Cellar/bridge/HEAD-<sha>, a Cellar of
    # /opt/homebrew or /usr/local -- what the app's install_method() resolves
    # against to detect Homebrew.
    virtualenv_install_with_resources

    # A Homebrew install has no PEP 610 direct_url.json, so stamp the resolved
    # commit into the sentinel installed_sha() falls back to. buildpath is the
    # git checkout, so rev-parse gives the exact full SHA.
    sha = Utils.safe_popen_read("git", "-C", buildpath, "rev-parse", "HEAD").strip
    build_py = Dir[libexec/"lib/python*/site-packages/bridge/_build.py"].first
    (Pathname.new(build_py)).atomic_write(%Q(COMMIT_SHA = "#{sha}"\n)) if build_py && sha.match?(/\A[0-9a-f]{40}\z/)
  end

  test do
    # No network, no LaunchAgent: just prove the entry point runs and reports
    # a version line. The install step stamped the resolved commit into
    # _build.py, so --version reports a real SHA.
    assert_match(/\d|unknown/i, shell_output("#{bin}/bridge --version"))
  end
end
```

- [ ] **Step 2: Verify Ruby style**

Run: `brew style mit112/bridge/bridge`
Expected: `1 file inspected, no offenses detected` (or the path to the local symlinked formula with no offenses).

- [ ] **Step 3: Verify audit sees a valid HEAD-only formula**

Run: `brew audit --strict mit112/bridge/bridge`
Expected: passes. A `HEAD-only formula` informational note is acceptable and expected (there is no stable release on purpose). If audit errors that resources are missing for the Python deps, that is resolved by Task 4 — record the message and continue.

- [ ] **Step 4: Commit**

```bash
cd ~/dev/homebrew-bridge
git rm --cached Formula/.keep 2>/dev/null || true
rm -f Formula/.keep
git add Formula/bridge.rb
git commit -m "Add base HEAD-only bridge formula"
```

---

### Task 4: Generate and paste the dependency resources

**Files:**
- Modify: `~/dev/homebrew-bridge/Formula/bridge.rb` (between the GENERATED-RESOURCES markers)

**Interfaces:**
- Consumes: `tools/brew_resources.py` (Task 2) and the base formula (Task 3).
- Produces: a formula whose marker block contains one `resource` stanza per runtime dependency, each with a `files.pythonhosted.org` sdist `url` and a bare 64-hex `sha256`, taken verbatim from `uv.lock`. Regenerating produces byte-identical output for an unchanged lock (the drift invariant Task 7 enforces).

- [ ] **Step 1: Generate the stanzas**

```bash
cd /Users/mitsheth/dev/bridge
uv run python tools/brew_resources.py uv.lock
```

Expected: ~21 stanzas printed to stdout. Each looks exactly like this (the first two are shown with the real values from the current `uv.lock` — the generator emits them verbatim, so these are not placeholders):

```ruby
  resource "anyio" do
    url "https://files.pythonhosted.org/packages/61/cc/a381afa6efea9f496eff839d4a6a1aed3bfafc7b3ab4b0d1b243a12573dd/anyio-4.14.2.tar.gz"
    sha256 "cfa139f3ed1a23ee8f88a145ddb5ac7605b8bbfd8592baacd7ce3d8bb4313c7f"
  end

  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/8a/02/91e3416a8fdd715abb903a952a6bec7cdd8d14eed55d415fc8595524c319/fastapi-0.141.1.tar.gz"
    sha256 "e8822fc40db1e1858054d7a949a888695bc9bdce70139178e33bd2871a453ca1"
  end
```

The complete set (sorted) covers, at minimum: `anyio`, `annotated-doc`, `annotated-types`, `click`, `fastapi`, `h11`, `httptools`, `idna`, `jinja2`, `markupsafe`, `pydantic`, `pydantic-core`, `python-dotenv`, `pyyaml`, `starlette`, `typing-extensions`, `typing-inspection`, `uvicorn`, `uvloop`, `watchfiles`, `websockets`. (`colorama` appears only if the lock records it as non-Windows-gated; the generator emits whatever is in the runtime closure.)

- [ ] **Step 2: Paste the output between the markers**

Replace the two comment lines describing regeneration is optional — keep the `do not edit by hand` and regenerate-command comment lines, then paste the generated block immediately below them, before `# END GENERATED RESOURCES`. The block must sit inside the class body, indented two spaces (the generator already indents).

Deterministic way to splice it (avoids hand-editing mistakes):

```bash
cd /Users/mitsheth/dev/bridge
GEN="$(uv run python tools/brew_resources.py uv.lock)"
python3 - "$GEN" <<'PY'
import re, sys
from pathlib import Path
gen = sys.argv[1]
f = Path.home() / "dev/homebrew-bridge/Formula/bridge.rb"
text = f.read_text()
begin = "  # BEGIN GENERATED RESOURCES"
end = "  # END GENERATED RESOURCES"
header = (
    begin + " -- do not edit by hand.\n"
    "  # Regenerate from the main repo with:\n"
    "  #   uv run python tools/brew_resources.py uv.lock\n"
    "  # and paste the output between these markers. CI enforces no drift.\n"
)
pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
f.write_text(pattern.sub(header + "\n" + gen + "\n" + end, text))
print("spliced")
PY
```

Expected: `spliced`. Open the formula and confirm the resources sit between the markers.

- [ ] **Step 3: Re-run style and audit with resources present**

Run: `brew style mit112/bridge/bridge && brew audit --strict mit112/bridge/bridge`
Expected: no offenses; audit passes. If audit reports resources out of order, that is a generator bug — the generator sorts by name, so re-run Step 1 and re-splice rather than hand-reordering.

- [ ] **Step 4: Commit**

```bash
cd ~/dev/homebrew-bridge
git add Formula/bridge.rb
git commit -m "Generate dependency resources from uv.lock"
```

---

### Task 5: Install locally and verify install-method compatibility

**Files:**
- No file changes. This task is verification only; its "test" is the brew lifecycle and the executable-resolution check the app's `install_method()` depends on.

**Interfaces:**
- Consumes: the tapped local formula (Task 4).
- Produces: a proven `brew install --HEAD bridge` that lands `bridge` under a Cellar keg and links it into the prefix `bin` — the layout `install_method()` resolves against.

- [ ] **Step 1: Install from HEAD (local tap)**

```bash
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
  brew install --HEAD --verbose mit112/bridge/bridge
```

Expected: fetches `main`, builds a venv under `$(brew --prefix)/Cellar/bridge/HEAD-<sha>/libexec`, links `bridge` into `$(brew --prefix)/bin`. Native-build resources (`pydantic-core`, `uvloop`, `watchfiles`, `httptools`, `websockets`, `markupsafe`) compile from sdist — expect compiler output, then success.

- [ ] **Step 2: Verify the entry point runs**

```bash
bridge --version
```

Expected: a version line including a short SHA and install method (per Section C). At minimum, non-empty output and exit 0.

- [ ] **Step 3: Verify the Cellar layout `install_method()` needs**

```bash
readlink "$(command -v bridge)"
```

Expected: resolves into `.../Cellar/bridge/HEAD-<sha>/libexec/bin/bridge` under `/opt/homebrew` (Apple Silicon) or `/usr/local` (Intel). This is exactly what the app resolves the executable against to report `Homebrew` as the install method. Record the resolved path.

- [ ] **Step 4: Run `brew test`**

```bash
brew test mit112/bridge/bridge
```

Expected: PASS (the `test do` block runs `bridge --version` and asserts a version-ish match).

- [ ] **Step 5: No commit (verification only). Note the observed keg path in the PR description.**

---

### Task 6: Verify the `brew upgrade --fetch-HEAD` update path

**Files:**
- No file changes. Verification only; documents the one true update path and that plain `brew upgrade` is a no-op for HEAD.

**Interfaces:**
- Consumes: the installed HEAD keg (Task 5).
- Produces: proof that `brew upgrade --fetch-HEAD mit112/bridge/bridge` re-resolves `main` and reinstalls, and that plain `brew upgrade` does not advance the HEAD formula (matching the Section B engine's chosen command).

- [ ] **Step 1: Confirm plain `brew upgrade` is a no-op for the HEAD formula**

```bash
HOMEBREW_NO_AUTO_UPDATE=1 brew upgrade mit112/bridge/bridge || true
```

Expected: reports the formula as up-to-date / not upgraded even if `main` moved — because a plain upgrade will not re-fetch a HEAD build. This is why Bridge ships its own update path; record the message.

- [ ] **Step 2: Run the real update command (what `bridge update` invokes)**

```bash
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 \
  brew upgrade --fetch-HEAD mit112/bridge/bridge
```

Expected: re-fetches `main` HEAD and rebuilds if the remote SHA changed since install; if `main` has not moved since Task 5, Homebrew reports it already up-to-date at the current HEAD — that is the correct, expected outcome on an unchanged remote.

- [ ] **Step 3: Re-verify after upgrade**

```bash
bridge --version && brew test mit112/bridge/bridge
```

Expected: version line prints; `brew test` PASSES against the (possibly rebuilt) keg.

- [ ] **Step 4: No commit (verification only).**

---

### Task 7: Resource-drift guard in the main repo's CI

**Files:**
- Modify: `/Users/mitsheth/dev/bridge/.github/workflows/ci.yml` — this file **already exists** from the public-flip-readiness plan (Task 1), which created it with `name: CI` and a single `test` job. Add the `brew-resources-drift` job to the existing `jobs:` map. Do NOT recreate the file or change its header, triggers, or `test` job.

> **Cross-plan ordering:** this task depends on the readiness plan's Task 1 (which authors `ci.yml`) and on this Homebrew plan's Tasks 2 + 4 (the generator and the spliced tap formula). Execute it after those.

**Interfaces:**
- Consumes: `tools/brew_resources.py` and `uv.lock` in the main repo; the committed `Formula/bridge.rb` fetched from the tap repo; the existing `.github/workflows/ci.yml` and its `test` job.
- Produces: a second CI job (`brew-resources-drift`) that fails if the tap's committed GENERATED-RESOURCES block differs from a fresh regeneration off the current `uv.lock` — the enforcement that keeps the tap from silently drifting. The `test` job is left untouched, so branch protection's required `test` context still holds.

- [ ] **Step 1: Add the drift-guard job to the existing `ci.yml`**

The file already contains `name: CI`, the `concurrency`/`permissions` header, and the `test` job. Add the `brew-resources-drift` job under the existing `jobs:` key so the file reads exactly like this (header + `test` job unchanged, new job appended — `setup-uv@v5` matches Task 1's pin):

```yaml
name: CI

on:
  push:
    branches: ["**"]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  test:
    name: test
    runs-on: macos-latest
    steps:
      - name: Check out the repository
        uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.13"
      - name: Sync dependencies (with dev extras)
        run: uv sync --extra dev
      - name: Run the test suite under a clean HOME (hermetic)
        env:
          HOME: ${{ runner.temp }}/clean-home
        run: |
          mkdir -p "$HOME"
          uv run pytest

  brew-resources-drift:
    name: brew-resources-drift
    runs-on: macos-latest
    steps:
      - name: Check out the main repo
        uses: actions/checkout@v4
      - name: Check out the tap
        uses: actions/checkout@v4
        with:
          repository: mit112/homebrew-bridge
          path: _tap
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.13"
      - name: Regenerate resources from uv.lock
        run: uv run python tools/brew_resources.py uv.lock > _generated_resources.rb
      - name: Extract committed resource block from the tap
        run: |
          awk '/# BEGIN GENERATED RESOURCES/{f=1;next} /# END GENERATED RESOURCES/{f=0} f' \
            _tap/Formula/bridge.rb \
            | sed '/^\s*#/d' \
            | sed '/^[[:space:]]*$/d' > _committed_resources.rb
      - name: Fail on drift
        run: |
          sed '/^[[:space:]]*$/d' _generated_resources.rb > _gen_clean.rb
          if ! diff -u _committed_resources.rb _gen_clean.rb; then
            echo "::error::Formula resources drifted from uv.lock. Run tools/brew_resources.py and re-splice into the tap." >&2
            exit 1
          fi
          echo "Resources match uv.lock."
```

> Note: `brew-resources-drift` is intentionally **not** added to branch protection's required checks (only `test` is required), because it checks out the tap repo and would block main-repo PRs on tap availability. It runs and reports, but does not gate.

- [ ] **Step 2: Lint the workflow YAML locally**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('/Users/mitsheth/dev/bridge/.github/workflows/ci.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 3: Dry-run the drift logic locally (the CI's core assertion)**

```bash
cd /Users/mitsheth/dev/bridge
uv run python tools/brew_resources.py uv.lock | sed '/^[[:space:]]*$/d' > /tmp/gen_clean.rb
awk '/# BEGIN GENERATED RESOURCES/{f=1;next} /# END GENERATED RESOURCES/{f=0} f' \
  ~/dev/homebrew-bridge/Formula/bridge.rb | sed '/^\s*#/d' | sed '/^[[:space:]]*$/d' > /tmp/committed.rb
diff -u /tmp/committed.rb /tmp/gen_clean.rb && echo "NO DRIFT"
```

Expected: `NO DRIFT` (empty diff), because Task 4 spliced the generator's exact output. A non-empty diff here means the splice in Task 4 was hand-edited — re-splice.

- [ ] **Step 4: Commit**

```bash
cd /Users/mitsheth/dev/bridge
git add .github/workflows/ci.yml
git commit -m "Fail CI when Homebrew resources drift from uv.lock"
```

---

### Task 8: Install / upgrade / test / audit lifecycle CI in the tap repo

**Files:**
- Create: `~/dev/homebrew-bridge/.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the tap's own `Formula/bridge.rb`.
- Produces: a `macos-latest` CI that runs `brew audit --strict`, `brew style`, `brew install --HEAD`, a repeat `brew upgrade --fetch-HEAD`, and `brew test` — the lifecycle the spec requires, with the same `HOMEBREW_NO_AUTO_UPDATE`/`HOMEBREW_NO_INSTALL_CLEANUP` env the app's update engine uses.

- [ ] **Step 1: Write the tap CI workflow**

Create `~/dev/homebrew-bridge/.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

env:
  HOMEBREW_NO_AUTO_UPDATE: "1"
  HOMEBREW_NO_INSTALL_CLEANUP: "1"

jobs:
  formula:
    runs-on: macos-latest
    steps:
      - name: Checkout tap
        uses: actions/checkout@v4
      - name: Tap this checkout
        run: |
          brew tap-new mit112/bridge --no-git || true
          TAP_DIR="$(brew --repository)/Library/Taps/mit112/homebrew-bridge"
          rm -rf "$TAP_DIR"
          ln -s "$GITHUB_WORKSPACE" "$TAP_DIR"
      - name: Style
        run: brew style mit112/bridge/bridge
      - name: Audit (strict)
        run: brew audit --strict mit112/bridge/bridge
      - name: Install from HEAD
        run: brew install --HEAD --verbose mit112/bridge/bridge
      - name: Verify entry point
        run: bridge --version
      - name: Verify Cellar layout (install_method compatibility)
        run: |
          resolved="$(readlink "$(command -v bridge)")"
          echo "$resolved"
          echo "$resolved" | grep -Eq "/Cellar/bridge/HEAD-" \
            || { echo "::error::bridge not under a Cellar keg"; exit 1; }
      - name: Upgrade via fetch-HEAD (the bridge-update path)
        run: brew upgrade --fetch-HEAD mit112/bridge/bridge
      - name: Test
        run: brew test mit112/bridge/bridge
```

- [ ] **Step 2: Lint the workflow YAML locally**

Run: `python3 -c "import yaml; yaml.safe_load(open('$HOME/dev/homebrew-bridge/.github/workflows/ci.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 3: Dry-run the audit + style steps locally (the CI's static gates)**

```bash
brew style mit112/bridge/bridge && brew audit --strict mit112/bridge/bridge && echo "STATIC GATES PASS"
```

Expected: `STATIC GATES PASS`. (The install/upgrade/test steps were exercised locally in Tasks 5–6; CI repeats them on a clean runner.)

- [ ] **Step 4: Commit**

```bash
cd ~/dev/homebrew-bridge
git add .github/workflows/ci.yml
git commit -m "Add install/upgrade/test/audit lifecycle CI"
```

---

### Task 9: Add the Homebrew section to the main-repo README

**Files:**
- Modify: `/Users/mitsheth/dev/bridge/README.md` (insert a Homebrew block inside "## Quick start", after the `uv tool install` block around line 9-23; and add one line to the "## Uninstall" / rollback area)

**Interfaces:**
- Consumes: nothing.
- Produces: README documentation of the tap install, the `bridge update` upgrade path, and that Homebrew rollback is unsupported (uv fallback) — matching the spec's "README gets a Homebrew section alongside the `uv` quick-start."

- [ ] **Step 1: Add the Homebrew subsection under Quick start**

In `/Users/mitsheth/dev/bridge/README.md`, immediately after the closing ``` of the `uv tool install` code block (after line 23, before the "`bridge setup` walks you through…" paragraph), insert:

````markdown
### Or install with Homebrew

```bash
brew tap mit112/bridge
brew install --HEAD bridge
```

Then continue with `bridge setup`, `bridge index`, `bridge open` as above.

Bridge tracks `main` HEAD, so a plain `brew upgrade` will **not** advance it.
To update, use Bridge's own updater — the panel shows an "update available"
nudge, or run:

```bash
bridge update
```

which runs `brew upgrade --fetch-HEAD mit112/bridge/bridge` for you.

**Rollback is not supported on the Homebrew path.** If an update leaves Bridge
broken, reinstall via `uv` instead:

```bash
brew uninstall bridge
uv tool install git+https://github.com/mit112/bridge
```
````

- [ ] **Step 2: Verify the README still renders as valid Markdown (no broken fences)**

Run: `python3 -c "t=open('/Users/mitsheth/dev/bridge/README.md').read(); assert t.count('\`\`\`') % 2 == 0, 'unbalanced code fences'; print('fences balanced')"`
Expected: `fences balanced`.

- [ ] **Step 3: Commit**

```bash
cd /Users/mitsheth/dev/bridge
git add README.md
git commit -m "Document Homebrew install and update path"
```

---

### Task 10: Push both repos and enable CI

**Files:**
- No file changes; publishing only.

**Interfaces:**
- Consumes: all prior commits in `~/dev/homebrew-bridge` and `/Users/mitsheth/dev/bridge` (branch `feat/share-autoupdate-homebrew`).
- Produces: the public tap with passing CI, and the main-repo branch pushed with its drift-guard job.

- [ ] **Step 1: ATTENDED — Mit runs this — push the tap repo**

```bash
cd ~/dev/homebrew-bridge
git push -u origin main
```

Expected: push succeeds; the tap's `formula` CI job starts on `macos-latest`.

- [ ] **Step 2: ATTENDED — Mit runs this — push the main-repo branch**

```bash
cd /Users/mitsheth/dev/bridge
git push -u origin feat/share-autoupdate-homebrew
```

Expected: push succeeds; the main repo's `brew-resources-drift` job runs.

- [ ] **Step 3: ATTENDED — Mit runs this — confirm CI is green**

```bash
gh run list --repo mit112/homebrew-bridge --limit 1
gh run list --repo mit112/bridge --limit 1 --branch feat/share-autoupdate-homebrew
```

Expected: both most-recent runs `completed / success`. If the tap's `Upgrade via fetch-HEAD` step is the only failure and reports "already up-to-date," that is acceptable on an unchanged HEAD.

- [ ] **Step 4: ATTENDED — Mit runs this — anonymous install smoke test (post-public-flip)**

Once `mit112/bridge` and `mit112/homebrew-bridge` are public, from a machine that has never tapped Bridge:

```bash
brew untap mit112/bridge 2>/dev/null || true
brew tap mit112/bridge && brew install --HEAD bridge && bridge --version
```

Expected: taps, installs, prints a version line — proving the exact README commands work for a stranger.
