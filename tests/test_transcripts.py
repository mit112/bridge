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


def test_rescan_with_no_changes_parses_nothing(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    first = scan(p)
    again = scan(p, start_offset=first.new_offset, prev=first.record)
    assert again.lines_parsed == 0
    assert again.new_offset == first.new_offset
    assert again.record.title == "Do the thing"


def test_rescan_parses_only_appended_lines(write_transcript, normal_session):
    sid, lines = normal_session
    p = write_transcript("s.jsonl", lines)
    first = scan(p)
    with p.open("a") as f:
        f.write(jline(type="ai-title", sessionId=sid, aiTitle="Renamed"))
        f.write(jline(type="user", sessionId=sid, isSidechain=False,
                      message={"role": "user", "content": "more"}))
    second = scan(p, start_offset=first.new_offset, prev=first.record)
    assert second.lines_parsed == 2          # not len(lines) + 2
    assert second.record.title == "Renamed"  # accumulated onto prev
    assert second.record.user_msgs == 2


def test_incremental_totals_match_full_scan(write_transcript):
    """Accumulation across the offset boundary must not lose or double-count.

    Both halves carry a user turn and an assistant turn with tokens, so an
    implementation that discards `prev` (losing the first half) or re-reads
    from byte 0 (counting the first half twice) produces different totals and
    fails here.
    """
    sid = "44444444-4444-4444-4444-444444444444"
    cwd = "/Users/mitsheth/dev/demo"

    def user(ts, text):
        return jline(type="user", sessionId=sid, isSidechain=False,
                     timestamp=ts, cwd=cwd, gitBranch="main",
                     message={"role": "user", "content": text})

    def assistant(ts, tin, tout):
        return jline(type="assistant", sessionId=sid, isSidechain=False,
                     timestamp=ts, cwd=cwd,
                     message={"role": "assistant", "model": "claude-opus-5",
                              "usage": {"input_tokens": tin, "output_tokens": tout}})

    first = [user("2026-07-30T10:00:00.000Z", "one"),
             assistant("2026-07-30T10:00:01.000Z", 1, 2)]
    second = [user("2026-07-30T10:00:02.000Z", "two"),
              assistant("2026-07-30T10:00:03.000Z", 10, 20),
              jline(type="ai-title", sessionId=sid, aiTitle="Both halves")]

    p = write_transcript("s.jsonl", first)
    partial = scan(p)
    # The pre-offset half must really carry totals, or this test decays again.
    assert partial.record.user_msgs == 1
    assert partial.record.tokens_in == 1

    with p.open("a") as f:
        f.write("".join(second))

    incremental = scan(p, start_offset=partial.new_offset, prev=partial.record)
    full = scan(p)

    for field in ("user_msgs", "assistant_msgs", "tokens_in", "tokens_out",
                  "title", "started_at", "ended_at"):
        assert getattr(incremental.record, field) == getattr(full.record, field), field

    # State the arithmetic being protected, so a regression names itself.
    assert full.record.user_msgs == 2
    assert full.record.assistant_msgs == 2
    assert full.record.tokens_in == 11
    assert full.record.tokens_out == 22
    assert incremental.lines_parsed == 3


# --- One API response, several JSONL entries -------------------------------
#
# Claude writes one API response as several assistant entries (thinking, text,
# tool_use), each repeating that response's `usage` snapshot verbatim. Summing
# them inflates every token total. Measured across 60 real transcripts before
# this was written: 1052 multi-entry requestIds, every one contiguous, every one
# carrying byte-identical usage, and naive summation ran 199% high.

SID = "22222222-2222-2222-2222-222222222222"


def _assistant(*, req=None, tin=0, tout=0, sidechain=False, ts="2026-07-30T10:00:01.000Z"):
    kw = dict(
        type="assistant", sessionId=SID, isSidechain=sidechain, timestamp=ts,
        cwd="/Users/mitsheth/dev/demo",
        message={"role": "assistant", "model": "claude-opus-5",
                 "usage": {"input_tokens": tin, "output_tokens": tout}},
    )
    if req is not None:
        kw["requestId"] = req
    return jline(**kw)


def test_one_response_split_across_entries_counts_its_usage_once(write_transcript):
    """The thinking/text/tool_use entries of one response repeat one usage."""
    p = write_transcript("s.jsonl", [
        _assistant(req="req_A", tin=100, tout=50),
        _assistant(req="req_A", tin=100, tout=50),
        _assistant(req="req_A", tin=100, tout=50),
    ])
    rec = scan(p).record
    assert (rec.tokens_in, rec.tokens_out) == (100, 50)


def test_distinct_request_ids_are_each_counted(write_transcript):
    """Dedup must not collapse genuinely separate API responses."""
    p = write_transcript("s.jsonl", [
        _assistant(req="req_A", tin=100, tout=50),
        _assistant(req="req_B", tin=7, tout=3),
    ])
    rec = scan(p).record
    assert (rec.tokens_in, rec.tokens_out) == (107, 53)


def test_entries_with_no_request_id_are_each_counted(write_transcript):
    """Older transcripts have no requestId; those must still sum individually."""
    p = write_transcript("s.jsonl", [
        _assistant(tin=10, tout=5),
        _assistant(tin=10, tout=5),
    ])
    rec = scan(p).record
    assert (rec.tokens_in, rec.tokens_out) == (20, 10)


def test_an_entry_without_usage_does_not_claim_the_request_id(write_transcript):
    """Otherwise the no-usage entry consumes the id and the real usage is dropped."""
    no_usage = jline(type="assistant", sessionId=SID, isSidechain=False,
                     requestId="req_A", timestamp="2026-07-30T10:00:01.000Z",
                     message={"role": "assistant", "model": "claude-opus-5"})
    p = write_transcript("s.jsonl", [no_usage, _assistant(req="req_A", tin=100, tout=50)])
    rec = scan(p).record
    assert (rec.tokens_in, rec.tokens_out) == (100, 50)


def test_sidechain_usage_is_deduped_too(write_transcript):
    """Sidechain tokens are the same defect in the same function."""
    p = write_transcript("s.jsonl", [
        _assistant(req="req_S", tin=10, tout=5, sidechain=True),
        _assistant(req="req_S", tin=10, tout=5, sidechain=True),
    ])
    rec = scan(p).record
    assert rec.sidechain_tokens == 15


def test_dedup_survives_an_incremental_scan_boundary(write_transcript):
    """The boundary can land mid-response: a live transcript is rescanned often.

    Without persisted dedup state this is exactly where the triple-count
    reappears, and only for actively-running sessions -- the ones on the card.
    """
    p = write_transcript("s.jsonl", [_assistant(req="req_A", tin=100, tout=50)])
    first = scan(p)
    assert first.record.tokens_in == 100

    with p.open("a") as f:
        f.write(_assistant(req="req_A", tin=100, tout=50))

    incremental = scan(p, start_offset=first.new_offset, prev=first.record)
    assert (incremental.record.tokens_in, incremental.record.tokens_out) == (100, 50)
    assert incremental.record.tokens_in == scan(p).record.tokens_in
