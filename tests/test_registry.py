from bridge.registry import display_name, is_noise, transcript_files


def test_hides_known_noise_directories():
    for name in [
        "-private-tmp-ecc-analysis",
        "-Users-mitsheth--claude",
        "-Users-mitsheth--local-share-ecc-homunculus",
        "-Users-mitsheth--local-share-ecc-homunculus-projects-047e75c52e2a",
        "-Volumes-mit-immich",
    ]:
        assert is_noise(name) is True, name


def test_keeps_real_projects():
    for name in [
        "-Users-mitsheth-dev-projectY",
        "-Users-mitsheth-dev-Job-apps",
        "-Users-mitsheth-dev-StreakSync",
    ]:
        assert is_noise(name) is False, name


def test_display_name_is_last_path_segment():
    assert display_name("/Users/mitsheth/dev/projectY") == "projectY"
    assert display_name("/Users/mitsheth/dev/Job apps") == "Job apps"
    assert display_name("/Users/mitsheth/dev/projectY/boardwatch") == "boardwatch"


def test_display_name_survives_trailing_slash():
    assert display_name("/Users/mitsheth/dev/demo/") == "demo"


def test_transcript_files_skips_noise_dirs(tmp_path):
    good = tmp_path / "-Users-mitsheth-dev-demo"
    good.mkdir()
    (good / "a.jsonl").write_text("")
    bad = tmp_path / "-private-tmp-ecc-analysis"
    bad.mkdir()
    (bad / "b.jsonl").write_text("")
    found = transcript_files(tmp_path)
    assert [p.name for p in found] == ["a.jsonl"]


def test_transcript_files_missing_dir_returns_empty(tmp_path):
    assert transcript_files(tmp_path / "nope") == []
