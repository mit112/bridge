"""Bridge: the panel and CLI for handing sessions between Claude runs.

`__version__` is resolved from installed package metadata so it tracks the one
number in `pyproject.toml` rather than a second copy that could drift. The
`PackageNotFoundError` fallback covers running straight from a source tree that
was never installed (a `git clone` with no `uv sync`).
"""

import logging
import os
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("bridge")
except PackageNotFoundError:  # not installed -- running from a bare checkout
    __version__ = "0.1.0"


def configure_logging() -> None:
    """Give records a timestamp, level and module name, once per process.

    Nothing in Bridge called `basicConfig`, so every `getLogger(__name__)` record
    fell through to `logging.lastResort` -- printed bare, with no way to tell when
    it happened, how bad it was, or which module emitted it. `basicConfig` is a
    no-op once the root logger has a handler, so calling this from both the CLI
    and the serve entrypoint configures whichever runs first and leaves the other
    a harmless second call.

    `BRIDGE_LOG_LEVEL` (e.g. `DEBUG`) overrides the default INFO threshold; an
    unrecognised value falls back to INFO rather than raising.
    """
    level_name = os.environ.get("BRIDGE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
