# Public-Flip Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the CI status check, protect `main`, scan the full git history for secrets and disclosure hazards, then flip `mit112/bridge` public and prove an anonymous install works — closing Section A, the gate that unblocks everything else in the share/auto-update/Homebrew spec.

**Architecture:** This is an operations plan, not a code change (except one new workflow file). Sequence is dependency-ordered: (1) add the CI workflow so a green status check exists; (2) scan the entire history — all refs, reflogs, stashes, blobs, workflows — stop-and-decide per hit before anything goes public; (3) Mit applies branch protection wiring the CI job as a required check; (4) Mit flips visibility public and we smoke-test an anonymous install. Steps 3 and 4 need Mit's GitHub auth and are marked ATTENDED.

**Tech Stack:** GitHub Actions on `macos-latest`, Python 3.13, `uv` (astral) for sync + run, pytest, `gh` CLI (2.97.0) + GitHub REST API, gitleaks + trufflehog for secret scanning.

## Global Constraints

- **macOS only.** Bridge uses `osascript` and LaunchAgents; CI runs on `macos-latest`. No Linux/Windows targets. (spec: "macOS-only"; README: "macOS only.")
- **Python ≥ 3.13.** `requires-python = ">=3.13"`; CI pins Python 3.13. (pyproject.toml:6)
- **uv-managed.** Dependencies install via `uv sync --extra dev`; commands run via `uv run …`. Never `pip install` directly. (README "Install from source")
- **`main` is the release channel, so it is protected:** required PR review, required CI status checks, signed commits, no force-push / linear history. (spec invariant 2, §A.2)
- **Bridge never installs the floating `@main`** — not relevant to Section A directly, but the CI check this plan creates is the "passed CI" precondition the nudge later depends on. (spec invariant 2)
- **Stop-and-decide per scan hit.** A discovered *live* secret is **revoked**, not just history-rewritten. Not squashing history otherwise. (spec §A.1)
- **No AI attribution** in commits/PRs/branches/tags/release notes; no `Co-Authored-By` or "Generated with" trailers. (user global CLAUDE.md → Git)
- Already verified, no task needed: `dist/` is gitignored (`dist/.gitignore` = `*`); `LICENSE` (MIT) already exists. (spec §A note)

---

### Task 1: CI workflow — the required status check

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: a GitHub Actions workflow with a single job whose **check-run context is `test`**. Task 3 (branch protection) references this exact string `test` as the required status check. The job runs `uv sync --extra dev` then `uv run pytest` on `macos-latest` with Python 3.13; the whole pytest suite is hermetic by design (`docs/ARCHITECTURE.md:219` — must pass under a clean `$HOME`), and the job forces a scratch `$HOME` so that property is actually exercised in CI rather than assumed.

- [ ] **Step 1: Install actionlint locally to validate the workflow before pushing**

Run:
```bash
brew install actionlint
```
Expected: actionlint on PATH (`actionlint --version` prints a version). This is the local "test" harness for the YAML — it fails first because the file does not exist yet.

- [ ] **Step 2: Run actionlint to verify it has nothing to check (the failing/empty state)**

Run:
```bash
cd /Users/mitsheth/dev/bridge && actionlint
```
Expected: exits 0 with no output because `.github/workflows/` has no files yet. This confirms the tool is wired before we author the file.

- [ ] **Step 3: Write the CI workflow**

Create `.github/workflows/ci.yml` with exactly this content:
```yaml
name: CI

on:
  push:
    branches: ["**"]
  pull_request:

# One in-flight run per ref; cancel superseded runs to keep the required
# check fast and unambiguous.
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
          # The suite must pass with no real ~/.bridge or ~/.claude corpus
          # present (docs/ARCHITECTURE.md). Point HOME at a throwaway dir so
          # CI actually exercises hermeticity instead of trusting the runner.
          HOME: ${{ runner.temp }}/clean-home
        run: |
          mkdir -p "$HOME"
          uv run pytest
```

- [ ] **Step 4: Run actionlint to verify the workflow is valid (now passes)**

Run:
```bash
cd /Users/mitsheth/dev/bridge && actionlint .github/workflows/ci.yml
```
Expected: exits 0 with no output (valid workflow, no shellcheck/expression errors).

- [ ] **Step 5: Sanity-check the exact command the workflow runs, locally**

Run:
```bash
cd /Users/mitsheth/dev/bridge && env HOME="$(mktemp -d)" uv run pytest -q
```
Expected: the full suite passes green under a fresh `$HOME` (this mirrors the CI step and confirms the hermetic run works on macOS). If it fails, fix the failure before proceeding — CI will report the same red.

- [ ] **Step 6: Commit and push to trigger the first CI run**

```bash
cd /Users/mitsheth/dev/bridge
git add .github/workflows/ci.yml
git commit -m "Add CI workflow: uv sync + hermetic pytest on macos-latest py3.13"
git push origin feat/share-autoupdate-homebrew
```

- [ ] **Step 7: Confirm the CI run is green and note the exact check name**

Run:
```bash
cd /Users/mitsheth/dev/bridge
gh run watch --exit-status
gh run view --json jobs --jq '.jobs[].name'
```
Expected: `gh run watch` exits 0 (all jobs passed). The job-name list prints `test` — this is the string Task 3 uses as the required status check context. If the run is red, open the log (`gh run view --log-failed`), fix, and re-push before continuing.

---

### Task 2: Full-history secret + disclosure scan (the gate)

**Files:**
- No repo files change. This task produces a decision, not a diff. Findings are triaged in-conversation with Mit; scan output may be written to the scratchpad, never committed.

**Interfaces:**
- Consumes: nothing from other tasks; runnable in parallel with Task 1.
- Produces: a go/no-go signal for Task 4 (the visibility flip). The flip MUST NOT happen until this task reports zero unresolved findings. History is **not** squashed unless a stop-and-decide hit forces a rewrite; a discovered *live* secret is revoked first, then optionally rewritten.

- [ ] **Step 1: Install the scanners**

Run:
```bash
brew install gitleaks trufflehog
```
Expected: `gitleaks version` and `trufflehog --version` both print versions.

- [ ] **Step 2: gitleaks over the full history including reflogs and stashes**

`--log-opts` is passed straight to `git log`; `--all` covers every ref (branches, tags, remotes), `--reflog` pulls in reflog-only commits, and `--full-history` keeps merge-hidden commits. Stashes are reachable via their reflog (`refs/stash`), which `--all --reflog` includes.
Run:
```bash
cd /Users/mitsheth/dev/bridge
gitleaks detect \
  --source . \
  --log-opts="--all --full-history --reflog" \
  --redact \
  --report-format json \
  --report-path /private/tmp/claude-501/-Users-mitsheth-dev-bridge/bc87af66-18fb-4a3b-a53a-7f48794eda7b/scratchpad/gitleaks.json \
  --verbose
```
Expected: `no leaks found` and exit code 0. **Stop-and-decide** on any non-zero exit: open `gitleaks.json`, classify each finding as live-secret / false-positive / dead-test-fixture.

- [ ] **Step 3: gitleaks over the workflow files explicitly (belt-and-suspenders)**

The tree scan above already covers `.github/workflows/`, but run a targeted no-git scan so a secret pasted into a workflow (e.g. a token literal) is caught even if it were only ever staged, never committed:
```bash
cd /Users/mitsheth/dev/bridge
gitleaks detect --no-git --source .github/workflows --redact --verbose
```
Expected: `no leaks found`, exit 0.

- [ ] **Step 4: trufflehog over the whole git history, verified + unknown results**

`trufflehog git file://.` walks every commit on every branch. `--results=verified,unknown` surfaces both confirmed-live secrets and unclassifiable ones (drops obvious false positives).
Run:
```bash
cd /Users/mitsheth/dev/bridge
trufflehog git file://. --results=verified,unknown --json \
  > /private/tmp/claude-501/-Users-mitsheth-dev-bridge/bc87af66-18fb-4a3b-a53a-7f48794eda7b/scratchpad/trufflehog.json
echo "exit: $?"
grep -c . /private/tmp/claude-501/-Users-mitsheth-dev-bridge/bc87af66-18fb-4a3b-a53a-7f48794eda7b/scratchpad/trufflehog.json
```
Expected: exit 0 and a `0` line count (no detector results). **Stop-and-decide** on any line: each JSON line is one finding with `DetectorName`, `Verified`, and `SourceMetadata` (commit + file). A `"Verified": true` result is a **live** secret — revoke it (rotate the credential at its provider) *before* any history rewrite, per spec §A.1.

- [ ] **Step 5: Enumerate every binary blob ever committed and eyeball it**

Secret scanners skip binary content; competitor names or keys can hide in a committed binary. List all blobs, flag the non-text ones by size/type, and inspect anything unexpected. The known-legitimate binaries are the bundled OFL webfonts under `src/bridge/static/fonts/`.
Run:
```bash
cd /Users/mitsheth/dev/bridge
git rev-list --all --objects \
  | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
  | awk '$1=="blob" && $2+0 > 20000 {print}' \
  | sort -k2 -n -r
```
Expected: the list is only the webfont files (`*.woff2`/`*.ttf` etc. under `static/fonts/`) and nothing surprising. **Stop-and-decide** on any unexplained binary: extract it (`git cat-file blob <sha> > /path/in/scratchpad`) and run `strings` on it (Step 6).

- [ ] **Step 6: Manual pass — embedded binary symbols + competitor-disclosure text**

Two flagged caveats from the OSS-readiness memory. First, run `strings` over the binary blobs surfaced in Step 5 to catch embedded symbol names / paths / author strings that shouldn't ship. Second, grep the working tree and full history text for competitor names and internal-disclosure phrasing.
Run:
```bash
cd /Users/mitsheth/dev/bridge
# (a) symbols inside each flagged binary blob — replace <sha> with blobs from Step 5
#     git cat-file blob <sha> | strings | less
# (b) competitor / internal-disclosure text across the whole history
git grep -I -n -i -E 'cursor|windsurf|copilot|codeium|internal[- ]only|do not (share|distribute)|confidential|proprietary' \
  $(git rev-list --all) -- 2>/dev/null | sort -u | head -50
```
Expected: no hit that actually discloses a competitor comparison or internal-only material. Ordinary prose (e.g. the word "internal" in an unrelated sentence) is fine — judge each. **Stop-and-decide** on a real disclosure: decide with Mit whether to edit-forward (public commit removing it) or rewrite history; do not proceed to the flip with an unresolved hit.

- [ ] **Step 7: Record the go/no-go and report to Mit**

Summarize: gitleaks clean (Steps 2-3), trufflehog clean (Step 4), binary blobs all accounted for (Steps 5-6), no disclosure text. If any finding required a decision, record what was done (revoked? rewritten? accepted as false-positive?). This summary is the gate signal for Task 4. **No commit** — this task changes no tracked files.

---

### Task 3: Branch protection on `main` — ATTENDED (Mit runs this)

**Files:**
- No repo files. Server-side GitHub configuration via `gh api`.

**Interfaces:**
- Consumes: the required status-check context string `test` from Task 1 (the job name reported by `gh run view --json jobs`).
- Produces: a protected `main` satisfying spec invariant 2 (required PR review, required status checks, signed commits, linear history / no force-push). This is the release-channel foundation the later update-nudge depends on.

> **ATTENDED — Mit runs this.** These calls mutate repo settings and need an authenticated `gh` session with admin rights on `mit112/bridge`. Run them yourself, Mit; the agent should not. Precondition: Task 1's CI run is green on at least one ref so GitHub knows the `test` check exists, and Task 2 reported go.

- [ ] **Step 1: Confirm auth and admin scope (ATTENDED)**

Run:
```bash
gh auth status
gh api repos/mit112/bridge --jq '.permissions.admin'
```
Expected: authenticated as the account owning `mit112/bridge`, and `.permissions.admin` prints `true`.

- [ ] **Step 2: Apply branch protection to `main` (ATTENDED)**

`strict: true` = branch must be up to date before merge; `contexts: ["test"]` wires Task 1's job as the required check; `required_pull_request_reviews` requires one approving review; `allow_force_pushes:false` + `required_linear_history:true` = no force-push and linear history; `required_signatures` (separate call in Step 3) enforces signed commits.
Run:
```bash
gh api -X PUT repos/mit112/bridge/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["test"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```
Expected: HTTP 200 with a JSON body echoing the protection settings. `restrictions: null` is required by the API (no user/team push allowlist). `enforce_admins:true` applies the rules to Mit too — the safe default for a release channel.

- [ ] **Step 3: Require signed commits on `main` (ATTENDED)**

Signed commits are a separate endpoint from the main protection payload:
```bash
gh api -X POST repos/mit112/bridge/branches/main/protection/required_signatures \
  -H "Accept: application/vnd.github+json"
```
Expected: HTTP 200 with `{"enabled": true, ...}`.

- [ ] **Step 4: Verify the protection reads back exactly as intended (ATTENDED)**

Run:
```bash
gh api repos/mit112/bridge/branches/main/protection \
  --jq '{
    checks: .required_status_checks.contexts,
    strict: .required_status_checks.strict,
    reviews: .required_pull_request_reviews.required_approving_review_count,
    linear: .required_linear_history.enabled,
    force_push: .allow_force_pushes.enabled,
    signatures: .required_signatures.enabled,
    admins: .enforce_admins.enabled
  }'
```
Expected exactly:
```json
{
  "checks": ["test"],
  "strict": true,
  "reviews": 1,
  "linear": true,
  "force_push": false,
  "signatures": true,
  "admins": true
}
```
If any value is off, re-run the relevant PUT/POST from Steps 2-3.

---

### Task 4: Visibility flip + anonymous-install smoke test — ATTENDED (Mit runs the flip)

**Files:**
- No repo files. GitHub visibility change + a throwaway install in a clean environment.

**Interfaces:**
- Consumes: green gate from Task 2, protected `main` from Task 3, green CI from Task 1.
- Produces: a public `mit112/bridge` and proof that the README quick-start line (`uv tool install git+https://github.com/mit112/bridge`) works with no auth.

> **ATTENDED — Mit runs the flip (Step 2).** The visibility change is irreversible-in-perception (once public, assume it was seen) and needs Mit's admin auth + explicit approval. The smoke test (Steps 3-5) is unattended but must run *after* the flip because it proves anonymous, unauthenticated access.

- [ ] **Step 1: Final pre-flip checklist (ATTENDED — confirm all three)**

Confirm, out loud, before flipping:
1. Task 1 CI is green on `main` (or the branch that will become the source of truth): `gh run list --branch main --limit 1`.
2. Task 2 reported go — no unresolved secret or disclosure finding.
3. Task 3 protection verifies exactly as in its Step 4.

Do not proceed unless all three hold.

- [ ] **Step 2: Flip the repository to public (ATTENDED)**

Run:
```bash
gh repo edit mit112/bridge --visibility public --accept-visibility-change-consequences
```
Expected: command succeeds; `gh repo view mit112/bridge --json visibility --jq .visibility` prints `public`. (`--accept-visibility-change-consequences` is required by `gh` for public flips in non-interactive use.)

- [ ] **Step 3: Anonymous-install smoke test — clean env, no auth**

Prove the README line works for a stranger. Run in a subshell with GitHub/uv auth stripped from the environment and a throwaway uv tool root so nothing pollutes the real install:
```bash
env -i HOME="$(mktemp -d)" PATH="/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin" \
  UV_TOOL_DIR="$(mktemp -d)" UV_TOOL_BIN_DIR="$(mktemp -d)" \
  uv tool install git+https://github.com/mit112/bridge
```
Expected: uv clones over anonymous HTTPS, builds the wheel, and reports `Installed 1 executable: bridge`. No credential prompt. (`env -i` clears `GITHUB_TOKEN`/`GH_TOKEN`/`~/.netrc` influence; the fresh `HOME` guarantees no cached git creds.)

- [ ] **Step 4: Confirm the installed executable runs**

Run (reuse the same throwaway `UV_TOOL_BIN_DIR` from Step 3 — substitute its path):
```bash
"$UV_TOOL_BIN_DIR/bridge" --version
```
Expected: prints a Bridge version line without traceback. This confirms the anonymously-installed artifact is actually runnable, not just downloaded.

- [ ] **Step 5: Tear down the throwaway install and report**

The temp dirs from Step 3 vanish on reboot, but uninstall cleanly to be tidy:
```bash
env UV_TOOL_DIR="$UV_TOOL_DIR" UV_TOOL_BIN_DIR="$UV_TOOL_BIN_DIR" uv tool uninstall bridge
```
Report to Mit: repo is public, anonymous `uv tool install git+https://github.com/mit112/bridge` succeeds and `bridge --version` runs. Section A is closed — the gate for §B/§C/§D is open. **No commit** — this task changes no tracked files.
