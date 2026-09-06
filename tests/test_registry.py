from pathlib import Path

from bridge.registry import (
    display_name,
    encode_path,
    is_noise,
    is_noise_path,
    nesting_parent,
    transcript_files,
)

HOME = Path("/Users/dev")
OTHER_HOME = Path("/Users/someone-else")


def test_encode_path_matches_claude_code_encoding():
    """`/`, `.` and space all collapse to `-`, which is what makes it lossy."""
    assert encode_path("/Users/dev") == "-Users-dev"
    assert encode_path("/Users/dev/.claude") == "-Users-dev--claude"
    assert encode_path("/Users/dev/Job apps") == "-Users-dev-Job-apps"


def test_hides_known_noise_directories():
    for name in [
        "-private-tmp-some-sandbox",
        "-Users-dev--claude",
        "-Users-dev--local-share-some-tool",
        "-Users-dev--local-share-some-tool-projects-047e75c52e2a",
        "-Volumes-external-drive",
    ]:
        assert is_noise(name, home=HOME) is True, name


def test_keeps_real_projects():
    for name in [
        "-Users-dev-dev-projectY",
        "-Users-dev-dev-Job-apps",
        "-Users-dev-dev-widget-app",
    ]:
        assert is_noise(name, home=HOME) is False, name


def test_display_name_is_last_path_segment():
    assert display_name("/Users/dev/dev/projectY") == "projectY"
    assert display_name("/Users/dev/dev/Job apps") == "Job apps"
    assert display_name("/Users/dev/dev/projectY/nested-app") == "nested-app"


def test_display_name_survives_trailing_slash():
    assert display_name("/Users/dev/dev/demo/") == "demo"


def test_transcript_files_skips_noise_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(HOME))
    good = tmp_path / "-Users-dev-dev-demo"
    good.mkdir()
    (good / "a.jsonl").write_text("")
    bad = tmp_path / "-private-tmp-some-sandbox"
    bad.mkdir()
    (bad / "b.jsonl").write_text("")
    found = transcript_files(tmp_path)
    assert [p.name for p in found] == ["a.jsonl"]


def test_transcript_files_missing_dir_returns_empty(tmp_path):
    assert transcript_files(tmp_path / "nope") == []


def test_container_directories_are_hidden():
    """$HOME and project-parent directories are not themselves projects."""
    for name in ["-Users-dev", "-Users-dev-dev", "-Users-dev-Documents"]:
        assert is_noise(name, home=HOME) is True, name


def test_container_match_is_exact_not_prefix():
    """Prefix matching on the encoded home would hide every real project.

    Also guards the reverse error: an ancestor-based rule would hide projectY
    because it contains nested-app.
    """
    for name in [
        "-Users-dev-dev-projectY",
        "-Users-dev-dev-projectY-nested-app",
        "-Users-dev-dev-Job-apps",
        "-Users-dev-Documents-client-work",
        "-Users-dev-Claude-Projects-regal",
    ]:
        assert is_noise(name, home=HOME) is False, name


# --- the portability contract: none of this may be tied to one username ------


def test_noise_rules_follow_the_running_users_home():
    """The whole point: another user's home is filtered, and ours is not.

    A hardcoded username makes every one of these assertions flip, which is
    what shipped an unusable first run to anyone who was not the author.
    """
    # Their home and its dotdirs are noise *for them*.
    assert is_noise("-Users-someone-else", home=OTHER_HOME) is True
    assert is_noise("-Users-someone-else--claude", home=OTHER_HOME) is True
    assert is_noise("-Users-someone-else-dev", home=OTHER_HOME) is True

    # And are NOT noise for us -- they are just unrecognised directories.
    assert is_noise("-Users-someone-else", home=HOME) is False
    assert is_noise("-Users-someone-else--claude", home=HOME) is False


def test_any_dotdir_under_home_is_noise_not_just_claude():
    """`.claude` was never special; hidden directories are simply not projects."""
    for name in ["-Users-dev--claude", "-Users-dev--config", "-Users-dev--cache-uv"]:
        assert is_noise(name, home=HOME) is True, name


def test_home_with_a_space_still_encodes_to_one_container():
    home = Path("/Users/Ada Lovelace")
    assert is_noise("-Users-Ada-Lovelace", home=home) is True
    assert is_noise("-Users-Ada-Lovelace--claude", home=home) is True
    assert is_noise("-Users-Ada-Lovelace-dev-widget", home=home) is False


def test_home_itself_is_never_a_project():
    """Anyone who has ever run `claude` from `~` would otherwise get a project
    card named after their login. Exactly home -- not "looks like a home"."""
    assert is_noise_path(HOME, home=HOME) is True
    assert is_noise_path(OTHER_HOME, home=HOME) is False


def test_is_noise_path_answers_for_the_containers_is_noise_already_knows():
    for path in [
        "/Users/dev/dev",
        "/Users/dev/Documents",
        "/Users/dev/.claude",
        "/Users/dev/.local/share/some-tool",
        "/private/tmp/some-sandbox",
        "/Volumes/external-drive",
    ]:
        assert is_noise_path(path, home=HOME) is True, path


def test_is_noise_path_keeps_real_projects():
    for path in ["/Users/dev/dev/projectY", "/Users/dev/dev/Job apps"]:
        assert is_noise_path(path, home=HOME) is False, path


def _repo(base: Path, *parts: str) -> Path:
    d = base.joinpath(*parts)
    (d / ".git").mkdir(parents=True)
    return d


def test_nesting_parent_finds_a_scratch_dir_inside_a_repo(tmp_path):
    parent = _repo(tmp_path, "boardwatch")
    child = parent / "2026-09-08-session"
    child.mkdir()
    assert nesting_parent(child, [str(parent)]) == str(parent)


def test_nesting_parent_never_fires_for_siblings(tmp_path):
    """Component-wise containment. `northwind-v2` is not inside `northwind`
    even though its path string starts with every character of it -- the case a
    `startswith` would silently hide the wrong project on."""
    a = _repo(tmp_path, "Job apps", "northwind")
    b = _repo(tmp_path, "Job apps", "northgate")
    prefixed = tmp_path / "Job apps" / "northwind-v2"
    prefixed.mkdir()
    assert nesting_parent(b, [str(a)]) is None
    assert nesting_parent(a, [str(b)]) is None
    assert nesting_parent(prefixed, [str(a)]) is None


def test_nesting_parent_leaves_a_nested_repo_of_its_own_alone(tmp_path):
    """A submodule or an independently cloned repo is a separate worktree to
    git, so it is a separate project here."""
    parent = _repo(tmp_path, "outer")
    child = _repo(tmp_path, "outer", "vendored")
    assert nesting_parent(child, [str(parent)]) is None


def test_nesting_parent_needs_the_ancestor_to_be_a_worktree_root(tmp_path):
    """Otherwise a registered container directory swallows everything below."""
    container = tmp_path / "dev"
    child = container / "widget-app"
    child.mkdir(parents=True)
    assert nesting_parent(child, [str(container)]) is None


def test_transcript_files_sorts_without_comparing_path_objects(tmp_path, monkeypatch):
    """The order is by path string; getting there via `Path.__lt__` is not free.

    Sorting 9,202 `Path` objects cost 8 ms of a ~106 ms reindex on the real
    corpus, because every comparison drives the pathlib protocol to build and
    cache a key per element. This asserts the shape -- zero `PurePath`
    comparisons -- rather than a wall clock, and pins the resulting order to
    the path strings so the cheap key can never quietly reorder the run.
    """
    import os
    from pathlib import PurePath

    monkeypatch.setenv("HOME", str(HOME))
    d1 = tmp_path / "-Users-dev-dev-alpha"
    d2 = tmp_path / "-Users-dev-dev-beta"
    for d, names in ((d2, ("z.jsonl", "a.jsonl")), (d1, ("m.jsonl", "b.jsonl"))):
        d.mkdir()
        for n in names:
            (d / n).write_text("")

    compares = []
    original = PurePath.__lt__
    monkeypatch.setattr(
        PurePath, "__lt__",
        lambda self, other: (compares.append(1), original(self, other))[1],
    )
    found = transcript_files(tmp_path)

    assert compares == [], f"{len(compares)} Path comparisons during the sort"
    assert [os.fspath(p) for p in found] == sorted(os.fspath(p) for p in found)
    assert [p.name for p in found] == ["b.jsonl", "m.jsonl", "a.jsonl", "z.jsonl"]
