"""Entry point: `python -m bridge <cmd>`.

`main` delegates to the CLI so `python -m bridge` and the `bridge` console script
accept the same commands. `run_db_command` holds the two that open the database;
the CLI imports it lazily so its own handoff path stays database-free.
"""

import argparse
import json
import logging
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from bridge import spool
from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store, now_epoch

log = logging.getLogger(__name__)


def run_db_command(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("index", "serve", "backfill"):
        p = sub.add_parser(name)
        p.add_argument("--projects-dir")
        p.add_argument("--db")
        p.add_argument("--spool-dir")
        if name == "backfill":
            # --dry-run is the default; writing must be asked for explicitly.
            p.add_argument("--write", action="store_true")
            p.add_argument("--dry-run", action="store_true")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    if args.cmd not in ("index", "serve", "backfill"):
        parser.print_usage(sys.stderr)
        return 2

    overrides: dict = {}
    if args.projects_dir:
        overrides["claude_projects_dir"] = Path(args.projects_dir)
    if args.db:
        overrides["db_path"] = Path(args.db)
    if args.spool_dir:
        overrides["spool_dir"] = Path(args.spool_dir)
    cfg = load(overrides)
    store = Store(cfg.db_path)

    if args.cmd == "index":
        def progress(done: int, total: int) -> None:
            if done % 250 == 0 or done == total:
                print(f"  {done}/{total} files", file=sys.stderr)

        stats = asdict(reindex(store, cfg, progress=progress))
        # After database loss the retained journal is the only copy of any
        # handoff, so indexing is where recovery happens. Guarded on an empty
        # table, so a routine index never resurrects a consumed handoff.
        stats["handoffs_rebuilt"] = spool.rebuild_if_empty(store, cfg.spool_dir).drained
        print(json.dumps(stats, indent=2))
        store.close()
        return 0

    if args.cmd == "backfill":
        from bridge import backfill

        stats = backfill.run(store, cfg, write=args.write and not args.dry_run)
        print(json.dumps(asdict(stats), indent=2))
        store.close()
        return 0

    import uvicorn

    from bridge import launcher, scheduler
    from bridge.api import create_app

    # Collect stale prompt files here rather than in `create_app`. A manual
    # `bridge serve` is the only recurring event this process has -- there is no
    # background loop to hang it off -- and the suite builds apps directly with
    # configs that still point `launches_dir` at the real `~/.bridge`, so a
    # collector inside `create_app` would delete real provenance under test.
    #
    # `OSError` only, matching the boot drain: an unreadable directory must not
    # stop the panel, but a programming error in the collector must still be
    # loud. Called through the module so the suite's guard can see the path.
    try:
        launcher.gc_prompt_files(cfg.launches_dir)
    except OSError:
        pass

    # Any row still `launching` here was claimed by a process that never got
    # to record a terminal status -- almost always this same command, killed
    # between claim and finish on its previous run. Reconciling before the
    # scheduler thread starts means the first tick never finds a stray row it
    # could double-fire.
    stray = store.reconcile_launching(now_epoch())
    if stray:
        log.info("reconciled %d stray 'launching' scheduled run(s)", stray)

    # The scheduler is a thread, not a second process, so it shares this same
    # `Store` connection rather than opening its own -- one sole writer, same
    # as the request handlers. It belongs here and not in `create_app`: the
    # suite builds apps directly for route tests, and none of them may spawn a
    # background thread that claims and fires real scheduled runs.
    stop = threading.Event()
    t = threading.Thread(
        target=scheduler.run_scheduler, args=(store, cfg, stop), daemon=True
    )
    t.start()

    try:
        uvicorn.run(create_app(store, cfg), host="127.0.0.1", port=cfg.port)
    finally:
        stop.set()
        t.join(timeout=5.0)
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    from bridge.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
