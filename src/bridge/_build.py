"""Commit-SHA sentinel. A plain committed module — NOT written by any build
hook. Editable/dev installs keep "unknown". The Homebrew formula overwrites
COMMIT_SHA with the resolved commit at install time (see the Homebrew plan);
git installs via uv don't need it because installed_sha() reads the commit
from the installer's PEP 610 direct_url.json instead."""

COMMIT_SHA = "unknown"
