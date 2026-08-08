# Bridge: Share-to-friends + Auto-update + Homebrew

**Date:** 2026-08-08
**Status:** Approved design, pre-implementation
**Reviewers:** gpt-5.6-sol (medium, read the repo), deepseek-v4-flash (doc only)

## Goal

Make Bridge shareable with a handful of friends who can (a) install it easily on
macOS, and (b) stay current as `main` advances — with a Homebrew path as well as the
existing `uv tool install`.

## Locked decisions

- Repo becomes **public** (after a history scan).
- Update model: background update-check + panel "update available" nudge + a
  `bridge update` command. **One-click is in scope now** (secured — see §C), not
  deferred; v2 follows v1 immediately.
- Versioning: **track `main` HEAD**; no per-push tags. But updates install the
  **exact resolved SHA**, never the floating `@main` ref.

## Non-negotiable invariants (from review)

1. Bridge never installs the floating `@main`. The update check resolves a SHA; the
   update installs **that exact SHA**; post-install verifies the running process
   reports it. (Kills TOCTOU + stale-cache installs.)
2. `main` is the release channel, so it is **protected**: required PR review,
   required CI status checks, signed commits, no force-push. An "update available"
   nudge only appears for a SHA that is a fast-forward descendant of the installed
   SHA and that passed CI.
3. The one-click update endpoint is **not** protected by the loopback `Host` check
   alone — that does not stop CSRF. It requires a per-install bearer token
   (`0600` file) **and** `Origin`/`Sec-Fetch-Site` validation **and** an explicit
   confirmation showing current→new SHA.
4. Every update is a **transaction**: previous SHA stored, lockfile prevents
   concurrent updates, failure rolls back (uv) or prints exact recovery (Homebrew),
   and a persistent state file lets the panel tell "updating…" from "crashed after
   update" across an SSE reconnect.

---

## A. Public-flip readiness (the gate)

Per the OSS-readiness memory, all prior blockers are merged; the open item is a
history review. Do it wider than `git log -p`:

1. **Secret scan** with gitleaks **and** trufflehog over **all refs + reflogs +
   stashes + binary blobs + `.github/workflows`**, not just current-branch `-p`.
   Manual pass for the two flagged caveats (embedded binary symbols,
   competitor-disclosure text). Stop-and-decide per hit; a discovered live secret is
   **revoked**, not just history-rewritten. Not squashing otherwise.
2. **Branch protection on `main`** (prerequisite for the release-channel invariant):
   required PR review, required status checks (the CI in §A.3), signed commits,
   linear history / no force-push.
3. **CI workflow** (`.github/workflows/ci.yml`): on push + PR, `uv sync --extra dev`
   then `uv run pytest` including the hermetic suite, on `macos-latest` +
   Python 3.13. This is the required status check.
4. After CI + protection land: **flip** `gh repo edit mit112/bridge --visibility public`,
   then an **anonymous-install smoke test** (`uv tool install git+…/bridge` from a
   clean env, no auth) confirming the README line works.

Note: `dist/` is already gitignored (`dist/.gitignore` = `*`); nothing to remove there.
`LICENSE` (MIT) already exists.

## B. `bridge update` — shared engine

One primitive the CLI and the panel button both call. Behavior:

- **Detect install method** by resolving the running executable against
  package-manager-owned prefixes (not substring guesses): uv-tool (under the
  uv-tools dir, cross-checked with `uv tool list`), Homebrew (under a Cellar/keg of
  **either** `/opt/homebrew` or `/usr/local`). **Fail explicitly** for pipx,
  editable/source, or ambiguous installs — never update whichever appears first.
- **uv path:** `uv tool install --force --reinstall
  git+https://github.com/mit112/bridge@<sha>` — the resolved SHA, non-interactive.
- **Homebrew path:** `brew upgrade --fetch-HEAD mit112/bridge/bridge` with
  `HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1` in the env.
- **Transaction:** acquire a lockfile (reject concurrent updates); record attempted
  SHA, previous SHA, method, start/end, exit status, log path; write a persistent
  update-state file.
- **Verify:** after install, confirm `bridge --version` / the health endpoint reports
  the expected SHA from a **fresh PID** (not just listener etime, not just curl 200).
  On mismatch/failure: **roll back** by reinstalling the previous SHA (uv) or printing
  exact `brew` recovery steps (Homebrew rollback is unsupported).
- **Restart** (only in the one-click flow, §C): re-bootstrap the LaunchAgent plist
  from the *new* install path before `kickstart`, because the plist pins an absolute
  `sys.executable` that a Homebrew/uv upgrade can move (`setup.py:292`, `:330`).

## C. Update check + panel nudge + secure one-click

**Knowing what we run — build-time SHA, injected or fail:**
- A hatchling build hook writes the commit SHA into **build output** (a generated
  `_build.py` in the wheel, not committed into `src/bridge`). It must receive/derive
  the exact SHA being built; if it cannot (no `.git`, sdist, ambiguous), the
  distributable build **fails** rather than shipping "unknown". Editable/dev installs
  legitimately report `unknown` → no nudge.
- `bridge --version` / `bridge status` shows short SHA + install method.

**Check — `git ls-remote`, not the GitHub API:**
- `git ls-remote https://github.com/mit112/bridge.git refs/heads/main` returns the
  remote SHA with **no 60/hr API limit**. Run it on a **separate bounded worker**,
  isolated from the 15s refresh loop (`__main__.py:158`, `:211`) so a network hang
  can't stall indexing or shutdown. Persistent ~30-min cache, jittered backoff,
  last-success/last-error timestamps.
- **Fail closed:** on timeout / network error / non-fast-forward, keep the last known
  result as *stale* and **never** infer "update available". Only surface a nudge when
  the remote SHA is a fast-forward **descendant** of the installed SHA (states:
  `current` / `behind` / `diverged` / `unknown`; nudge only on `behind`).
- **Privacy + opt-out:** README/setup document that Bridge contacts GitHub to check
  for updates; a persistent "disable update checks" setting turns it off. No GitHub
  token is collected or stored.

**Surface:** `update_state` + `latest_sha` on the status API the panel already polls
(no new polling channel).

**Nudge + one-click:**
- Dismissible banner in the panel shell, **dismissal keyed by the offered SHA** (a
  failed/dismissed update for SHA A must not suppress SHA B, and stays retryable with
  its error visible).
- Banner always offers the **copy-able `bridge update` command** (works when no
  LaunchAgent, and is the safe fallback).
- One-click button → `POST /api/update`, guarded by: **per-install bearer token**
  (read from a `0600` file the panel injects into its own page), **`Origin` +
  `Sec-Fetch-Site` = same-origin/none** rejection, and a **confirmation dialog
  showing current→new SHA**. The endpoint installs the **exact SHA the check
  surfaced** (not a re-resolved `@main`).
- The updater runs as a **separate one-shot LaunchAgent job**, not a child of the
  Bridge job — otherwise `kickstart -k` kills its own updater mid-flight. It
  reinstalls, re-bootstraps the plist, restarts, and writes the update-state file the
  panel reads on reconnect to distinguish success from crash.

## D. Homebrew tap (HEAD-tracking)

- New repo **`mit112/homebrew-bridge`**, `Formula/bridge.rb`.
- Python formula: `depends_on "python@3.13"`, `head "…/bridge.git", branch: "main"`,
  `virtualenv_install_with_resources`. **Resources are generated from `uv.lock`**
  (the same locked graph), not hand-pinned — hand-pinning drifts into a second
  manifest and `brew upgrade --fetch-HEAD` would install something broken. Regenerate
  whenever the **resolved** graph changes (every `uvicorn[standard]` transitive
  included), enforced in CI.
- Install: `brew tap mit112/bridge && brew install --HEAD bridge`.
- Plain `brew upgrade` won't advance a HEAD formula; the update path is Bridge's own
  nudge → `bridge update` → `brew upgrade --fetch-HEAD`.
- CI on `macos-latest`: `brew install --HEAD`, repeat `brew upgrade --fetch-HEAD`,
  `brew test`, `brew audit --strict`. Homebrew rollback documented as unsupported
  (uv fallback).
- README gets a Homebrew section alongside the `uv` quick-start.

---

## Sequencing

1. **A first** (CI + branch protection + history scan) — it's the gate and the
   release-channel foundation. Flip public after it's green.
2. **C's build-time SHA** + `bridge --version` SHA display (foundation for the check).
3. **B** (`bridge update` engine) — foundation for C's button.
4. **C** check + banner + secure one-click.
5. **D** Homebrew (independent; can parallel B/C once A is done).

## Testing

- Unit: install-method detection (uv/brew/pipx/editable/ambiguous), SHA-state
  classification (current/behind/diverged/unknown), transaction/lockfile, fail-closed
  check.
- Integration: build-SHA injection from wheel + sdist; post-install SHA verification;
  rollback on mismatch.
- Security: `/api/update` rejects missing/wrong token, cross-origin, and
  `Sec-Fetch-Site` mismatches (CSRF regression tests).
- Clean-VM/manual: one-shot-LaunchAgent updater survives parent `kickstart -k`; plist
  re-bootstrap after a path move; anonymous `uv tool install` post-flip; Homebrew
  install/upgrade lifecycle in CI.

## Risks

- One-click self-restart is the highest-risk surface even secured; the copy-command
  path is always available as fallback and is what unattended/no-LaunchAgent installs
  use.
- HEAD-tracking Homebrew resource generation is the highest **maintenance** surface;
  CI regeneration + audit is what keeps it from silently drifting.
