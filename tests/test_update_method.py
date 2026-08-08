import bridge.update as U


def test_install_method_uv(monkeypatch, tmp_path):
    exe = tmp_path / "uv" / "tools" / "bridge" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv" / "tools")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [])
    assert U.install_method() == "uv"


def test_install_method_brew_opt_homebrew(monkeypatch, tmp_path):
    cellar = tmp_path / "opt" / "homebrew" / "Cellar"
    exe = cellar / "bridge" / "0.1.0" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [cellar])
    assert U.install_method() == "brew"


def test_install_method_unknown_when_ambiguous(monkeypatch, tmp_path):
    exe = tmp_path / "somewhere" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [tmp_path / "cellar"])
    monkeypatch.setattr(U, "installed_sha", lambda: "a" * 40)
    assert U.install_method() == "unknown"


def test_install_method_dev_when_editable(monkeypatch, tmp_path):
    exe = tmp_path / "somewhere" / "bin" / "bridge"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(U, "_running_executable", lambda: exe)
    monkeypatch.setattr(U, "_uv_tools_dir", lambda: tmp_path / "uv")
    monkeypatch.setattr(U, "_brew_cellars", lambda: [tmp_path / "cellar"])
    monkeypatch.setattr(U, "installed_sha", lambda: None)
    assert U.install_method() == "dev"
