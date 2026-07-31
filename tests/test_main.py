from bridge.__main__ import main


def test_index_subcommand_runs_and_reports(tmp_path, capsys):
    projects = tmp_path / "projects"
    (projects / "-Users-mitsheth-dev-demo").mkdir(parents=True)
    code = main(["index", "--projects-dir", str(projects),
                 "--db", str(tmp_path / "b.db")])
    assert code == 0
    assert "files_seen" in capsys.readouterr().out


def test_unknown_subcommand_is_an_error(tmp_path):
    assert main(["nonsense"]) == 2
