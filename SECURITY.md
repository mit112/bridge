# Security

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

security contact: sheth.mit@northeastern.edu

Include what you found, how to reproduce it, and the impact you'd expect. A
response should follow within a few days.

## How updates are trusted

Bridge resolves the latest `main` commit over HTTPS from
`github.com/mit112/bridge` (`git ls-remote`) and installs that exact SHA —
never a floating ref. Trust rests on GitHub HTTPS plus a branch-protected
`main`; there is no separate artifact signing step.

The local panel binds to `127.0.0.1` only, and the update endpoint
(`POST /api/update`) requires a per-install token plus same-site checks, so a
page in your browser (or anything off the loopback interface) cannot trigger
an update or reach the panel at all.

## Scope

Bridge is a single-user, local-machine tool with no authentication on its
HTTP surface by design — see [README.md § Scope and safety](README.md#scope-and-safety)
for what that does and doesn't cover. Reports about the panel being reachable
from other processes running as the same local user are expected behavior,
not a vulnerability; reports about it being reachable from off-loopback are in
scope.
