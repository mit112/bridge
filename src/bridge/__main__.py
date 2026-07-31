"""Entry point: `python -m bridge index` and `python -m bridge serve`."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bridge.config import load
from bridge.indexer import reindex
from bridge.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="cmd")
    for name in ("index", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--projects-dir")
        p.add_argument("--db")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    if args.cmd not in ("index", "serve"):
        parser.print_usage(sys.stderr)
        return 2

    overrides: dict = {}
    if args.projects_dir:
        overrides["claude_projects_dir"] = Path(args.projects_dir)
    if args.db:
        overrides["db_path"] = Path(args.db)
    cfg = load(overrides)
    store = Store(cfg.db_path)

    if args.cmd == "index":
        def progress(done: int, total: int) -> None:
            if done % 250 == 0 or done == total:
                print(f"  {done}/{total} files", file=sys.stderr)

        stats = reindex(store, cfg, progress=progress)
        print(json.dumps(asdict(stats), indent=2))
        store.close()
        return 0

    import uvicorn

    from bridge.api import create_app

    uvicorn.run(create_app(store, cfg), host="127.0.0.1", port=cfg.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
