"""Entry point: `python -m bridge <cmd>`.

`main` delegates to the CLI so `python -m bridge` and the `bridge` console script
accept the same commands. `run_db_command` holds the two that open the database;
the CLI imports it lazily so its own handoff path stays database-free.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bridge import spool
from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store


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

    from bridge.api import create_app

    uvicorn.run(create_app(store, cfg), host="127.0.0.1", port=cfg.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    from bridge.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
