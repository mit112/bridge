from bridge.transcripts import scan
from tests.conftest import jline


def test_parses_normal_session(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    r = scan(p)
    rec = r.record
    assert rec.session_id == sid
    assert rec.project_path == "/Users/mitsheth/dev/demo"
    assert rec.title == "Do the thing"
    assert rec.last_prompt == "do the thing again"
    assert rec.git_branch == "main"
    assert rec.model == "claude-opus-5"
    assert rec.effort == "high"
    assert rec.user_msgs == 1
    assert rec.assistant_msgs == 1
    assert (rec.tokens_in, rec.tokens_out) == (10, 20)
    assert (rec.tokens_cache_create, rec.tokens_cache_read) == (30, 40)
    assert rec.started_at == "2026-07-30T10:00:00.000Z"
    assert rec.ended_at == "2026-07-30T10:00:05.000Z"
    assert rec.interrupted is False
    assert r.parse_errors == 0
    assert r.new_offset == p.stat().st_size


def test_malformed_line_is_counted_not_fatal(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines[:2] + ["{not json\n"] + lines[2:])
    r = scan(p)
    assert r.parse_errors == 1
    assert r.record.title == "Do the thing"  # scan continued past the bad line


def test_unknown_types_are_ignored(write_transcript, normal_session):
    sid, lines = normal_session
    extra = jline(type="totally-new-record-type", sessionId=sid, whatever=1)
    p = write_transcript("s.jsonl", lines + [extra])
    r = scan(p)
    assert r.parse_errors == 0
    assert r.record.session_id == sid


def test_missing_keys_tolerated(write_transcript):
    p = write_transcript("s.jsonl", [jline(type="assistant", sessionId="s2")])
    r = scan(p)
    assert r.record.session_id == "s2"
    assert r.record.model is None
    assert r.record.tokens_in == 0


def test_empty_file_yields_no_record(write_transcript):
    p = write_transcript("empty.jsonl", [])
    r = scan(p)
    assert r.record is None
    assert r.new_offset == 0


def test_truncated_final_line_is_not_an_error(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    with p.open("a") as f:
        f.write('{"type":"assis')  # session still being written
    r = scan(p)
    assert r.parse_errors == 0
    # offset stops before the partial line so the next scan re-reads it whole
    assert r.new_offset == sum(len(x.encode()) for x in lines)


def test_detached_head_branch_literal(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="user", sessionId="s3", isSidechain=False,
              timestamp="2026-07-30T10:00:00.000Z",
              cwd="/tmp/x", gitBranch="HEAD",
              message={"role": "user", "content": "hi"}),
    ])
    assert scan(p).record.git_branch == "HEAD"


def test_attachment_records_excluded_from_counts(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="attachment", sessionId="s4", attachment={"big": "x" * 100}),
        jline(type="user", sessionId="s4", isSidechain=False,
              message={"role": "user", "content": "hi"}),
    ])
    rec = scan(p).record
    assert rec.user_msgs == 1
    assert rec.assistant_msgs == 0


def test_sidechain_tokens_tracked_separately(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="assistant", sessionId="s5", isSidechain=True,
              message={"role": "assistant", "model": "claude-haiku-4-5",
                       "usage": {"input_tokens": 5, "output_tokens": 7}}),
    ])
    rec = scan(p).record
    assert rec.sidechain_tokens == 12
    assert rec.tokens_in == 0  # not counted in main totals
    assert rec.assistant_msgs == 0  # sidechain turns are not the session's turns


def test_interrupted_session_flagged(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="assistant", sessionId="s6", isSidechain=False,
              interruptedByShutdown=True, message={"role": "assistant"}),
    ])
    assert scan(p).record.interrupted is True


def test_nested_attachment_type_does_not_drop_the_record(write_transcript):
    """A byte-window prefilter would have dropped this record. Guard against that.

    Hand-written with compact separators (what real Claude Code emits) and the
    nested `"type":"attachment"` inside the first 64 bytes, with the record's
    own top-level type being `user`. Any future early-bytes fast path that
    skips on this pattern would silently lose a real turn, so this test must
    fail if one is reintroduced.
    """
    raw = (
        '{"attachment":{"type":"attachment"},"type":"user",'
        '"sessionId":"s11","isSidechain":false,"cwd":"/tmp/x",'
        '"timestamp":"2026-07-30T10:00:00.000Z",'
        '"message":{"role":"user","content":"hi"}}\n'
    )
    assert raw.index('"type":"attachment"') < 64  # the fixture's whole point
    p = write_transcript("s.jsonl", [raw])
    r = scan(p)
    assert r.record is not None
    assert r.record.user_msgs == 1
    assert r.record.project_path == "/tmp/x"
    assert r.parse_errors == 0


def test_aborted_mid_stream_flags_interrupted(write_transcript):
    p = write_transcript("s.jsonl", [
        jline(type="assistant", sessionId="s12", isSidechain=False,
              isAbortedMidStream=True, message={"role": "assistant"}),
    ])
    assert scan(p).record.interrupted is True
