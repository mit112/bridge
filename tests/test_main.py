import time

import pytest

from bridge.__main__ import main


def test_index_subcommand_runs_and_reports(tmp_path, capsys):
    projects = tmp_path / "projects"
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    code = main(["index", "--projects-dir", str(projects),
                 "--db", str(tmp_path / "b.db"),
                 "--spool-dir", str(tmp_path / "spool")])
    assert code == 0
    assert "files_seen" in capsys.readouterr().out


def test_unknown_subcommand_is_an_error(tmp_path):
    assert main(["nonsense"]) == 2


@pytest.fixture
def serve_cfg(tmp_path, monkeypatch):
    """A `serve` whose uvicorn never runs, over a throwaway `~/.bridge`.

    `main` has no `--launches-dir`, so the config is replaced wholesale. That is
    also the point of the exercise: without it the autouse guard fires, because
    a `serve` under test would otherwise garbage-collect the developer's real
    prompt files.
    """
    from bridge import __main__ as entry
    from bridge.config import load

    launches = tmp_path / "launches"
    launches.mkdir()
    cfg = load({"db_path": tmp_path / "s.db", "spool_dir": tmp_path / "spool",
                "launches_dir": launches,
                "claude_projects_dir": tmp_path / "projects"})
    monkeypatch.setattr(entry, "load", lambda overrides: cfg)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(kw))
    return launches, served


def test_serve_collects_stale_prompt_files_before_it_starts(serve_cfg):
    """The only recurring event under the manual-`bridge serve` uptime model.

    Nothing called `gc_prompt_files`, so `~/.bridge/launches` grew forever. It
    is deliberately NOT called from `create_app`: the suite builds apps directly
    with configs that still point `launches_dir` at the real `~/.bridge`, so a
    boot-time collector there would delete real provenance during tests.
    """
    launches, served = serve_cfg
    stale = launches / "old.prompt"
    stale.write_text("what ran two months ago")
    old = time.time() - 20 * 86400
    import os
    os.utime(stale, (old, old))
    fresh = launches / "new.prompt"
    fresh.write_text("what ran this morning")

    assert main(["serve"]) == 0
    assert served, "uvicorn must still be reached"
    assert not stale.exists(), "20 days is past the 14-day policy"
    assert fresh.exists(), "a live prompt file is provenance, not litter"


def test_an_uncollectable_launches_dir_does_not_stop_the_panel(serve_cfg,
                                                               monkeypatch):
    """Housekeeping is never the reason the panel refuses to start.

    Same policy as the boot drain, and the same reason: the collector is a
    convenience and the server is the point.
    """
    launches, served = serve_cfg
    from bridge import launcher

    def boom(*a, **k):
        raise OSError("launches dir is unreadable")

    monkeypatch.setattr(launcher, "gc_prompt_files", boom)
    assert main(["serve"]) == 0
    assert served, "the failure must not have reached uvicorn"
