import json
import bridge.update as U


def test_git_install_returns_vcs_commit(monkeypatch):
    payload = json.dumps({"url": U.REPO_URL,
                          "vcs_info": {"vcs": "git", "commit_id": "a" * 40}})
    monkeypatch.setattr(U, "_read_direct_url", lambda: payload)
    assert U.installed_sha() == "a" * 40


def test_editable_install_returns_none(monkeypatch):
    payload = json.dumps({"url": "file:///repo", "dir_info": {"editable": True}})
    monkeypatch.setattr(U, "_read_direct_url", lambda: payload)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "unknown")
    assert U.installed_sha() is None


def test_falls_back_to_build_sentinel(monkeypatch):
    monkeypatch.setattr(U, "_read_direct_url", lambda: None)  # no direct_url (e.g. Homebrew)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "b" * 40)
    assert U.installed_sha() == "b" * 40


def test_unknown_when_no_source(monkeypatch):
    monkeypatch.setattr(U, "_read_direct_url", lambda: None)
    monkeypatch.setattr(U._build, "COMMIT_SHA", "unknown")
    assert U.installed_sha() is None
