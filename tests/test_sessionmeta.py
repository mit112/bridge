import json
from pathlib import Path

from bridge import sessionmeta


def _write(meta_dir: Path, session_id: str, **fields) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    body = {"session_id": session_id}
    body.update(fields)
    (meta_dir / f"{session_id}.json").write_text(json.dumps(body), encoding="utf-8")


def test_read_populates_every_surfaced_field(tmp_path):
    _write(tmp_path, "s1", files_modified=3, lines_added=120, lines_removed=40,
           git_commits=2, git_pushes=1, duration_minutes=45, tool_errors=1,
           user_interruptions=2, uses_task_agent=True, uses_mcp=True,
           uses_web_search=True, uses_web_fetch=False)
    m = sessionmeta.read("s1", tmp_path)
    assert m is not None
    assert (m.files_modified, m.lines_added, m.lines_removed) == (3, 120, 40)
    assert (m.git_commits, m.git_pushes, m.duration_minutes) == (2, 1, 45)
    assert (m.tool_errors, m.user_interruptions) == (1, 2)
    assert m.uses_task_agent and m.uses_mcp and m.uses_web


def test_read_never_carries_token_fields(tmp_path):
    # Constraint 1: the transcript parse is the sole token authority.
    _write(tmp_path, "s1", input_tokens=999, output_tokens=888, files_modified=1)
    m = sessionmeta.read("s1", tmp_path)
    assert not hasattr(m, "input_tokens")
    assert not hasattr(m, "output_tokens")


def test_uses_web_is_true_when_either_web_flag_is_set(tmp_path):
    _write(tmp_path, "a", uses_web_search=True)
    _write(tmp_path, "b", uses_web_fetch=True)
    _write(tmp_path, "c")
    assert sessionmeta.read("a", tmp_path).uses_web is True
    assert sessionmeta.read("b", tmp_path).uses_web is True
    assert sessionmeta.read("c", tmp_path).uses_web is False


def test_missing_file_returns_none(tmp_path):
    assert sessionmeta.read("nope", tmp_path) is None


def test_malformed_json_returns_none(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert sessionmeta.read("bad", tmp_path) is None


def test_mismatched_session_id_returns_none(tmp_path):
    # A renamed/corrupt file whose body names a different session.
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s1.json").write_text(
        json.dumps({"session_id": "OTHER", "files_modified": 5}), encoding="utf-8")
    assert sessionmeta.read("s1", tmp_path) is None


def test_absent_keys_default_to_zero_and_false(tmp_path):
    _write(tmp_path, "s1")  # only session_id present
    m = sessionmeta.read("s1", tmp_path)
    assert m.files_modified == 0 and m.duration_minutes == 0
    assert m.uses_task_agent is False and m.uses_mcp is False and m.uses_web is False


def test_non_integer_field_is_tolerated_as_zero(tmp_path):
    _write(tmp_path, "s1", files_modified="lots")
    assert sessionmeta.read("s1", tmp_path).files_modified == 0


def test_has_signal_is_false_for_an_all_zero_session(tmp_path):
    _write(tmp_path, "s1")
    assert sessionmeta.read("s1", tmp_path).has_signal is False


def test_has_signal_is_true_when_any_fact_is_present(tmp_path):
    _write(tmp_path, "s1", duration_minutes=1)
    assert sessionmeta.read("s1", tmp_path).has_signal is True


def test_read_many_keeps_only_signal_bearing_ids(tmp_path):
    _write(tmp_path, "has", files_modified=2)
    _write(tmp_path, "empty")            # exists but no signal
    # "gone" has no file at all
    out = sessionmeta.read_many(["has", "empty", "gone"], tmp_path)
    assert set(out) == {"has"}
    assert out["has"].files_modified == 2
