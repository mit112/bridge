import bridge.update as U


def test_resolve_remote_sha_parses_ls_remote(monkeypatch):
    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = "deadbeef" * 5 + "\trefs/heads/main\n"
            stderr = ""
        return P()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    assert U.resolve_remote_sha() == "deadbeef" * 5


def test_resolve_remote_sha_none_on_failure(monkeypatch):
    def fake_run(cmd, **kw):
        class P:
            returncode = 128
            stdout = ""
            stderr = "could not read from remote"
        return P()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    assert U.resolve_remote_sha() is None


def test_resolve_remote_sha_none_on_timeout(monkeypatch):
    def boom(cmd, **kw):
        raise U.subprocess.TimeoutExpired(cmd, 8.0)
    monkeypatch.setattr(U.subprocess, "run", boom)
    assert U.resolve_remote_sha() is None


def test_resolve_remote_sha_none_on_non_hex_token(monkeypatch):
    # A 40-char token that isn't hex must not be accepted as a SHA.
    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = "z" * 40 + "\trefs/heads/main\n"
            stderr = ""
        return P()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    assert U.resolve_remote_sha() is None


def test_classify_current_when_equal():
    assert U.classify("a" * 40, "a" * 40) == "current"


def test_classify_unknown_when_missing():
    assert U.classify(None, "a" * 40) == "unknown"
    assert U.classify("a" * 40, None) == "unknown"


def test_classify_behind_when_remote_is_descendant():
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: True) == "behind"


def test_classify_diverged_when_not_descendant():
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: False) == "diverged"


def test_classify_unknown_when_ancestry_indeterminate():
    # Fail closed: an unknowable ancestry never infers an available update.
    assert U.classify("a" * 40, "b" * 40, is_ancestor=lambda i, r: None) == "unknown"
