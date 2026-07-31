"""Stream Claude Code JSONL transcripts into SessionRecords.

Design notes driven by the real corpus (9,229 files, 3.5 GB):
  * `attachment` records are ~62% of all lines and carry nothing we need, so
    they get a cheap prefix check before the JSON parse. If key order ever
    changes the check simply misses and we parse normally — correctness is
    never at stake, only speed.
  * Unknown `type` values and absent keys are normal across CLI versions.
  * A truncated final line means the session is still being written. It is not
    an error, and the returned offset stops before it so the next scan re-reads
    it whole.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from bridge.models import SessionRecord

_ATTACHMENT_HINT = b'"type":"attachment"'


@dataclass
class ScanResult:
    record: SessionRecord | None
    new_offset: int
    lines_parsed: int = 0
    parse_errors: int = 0


def scan(path: Path, start_offset: int = 0, prev: SessionRecord | None = None) -> ScanResult:
    """Read `path` from `start_offset` to the last complete line.

    `prev` lets an incremental scan accumulate onto an earlier result.
    """
    rec = replace(prev) if prev else None
    offset = start_offset
    lines_parsed = 0
    parse_errors = 0

    with path.open("rb") as f:
        f.seek(start_offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # partial trailing line; leave offset before it
            offset += len(raw)
            if _ATTACHMENT_HINT in raw[:64]:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                parse_errors += 1
                continue
            if not isinstance(obj, dict):
                parse_errors += 1
                continue
            lines_parsed += 1
            rec = _apply(rec, obj, str(path))

    return ScanResult(rec, offset, lines_parsed, parse_errors)


def _apply(rec: SessionRecord | None, obj: dict, path: str) -> SessionRecord | None:
    sid = obj.get("sessionId") or obj.get("session_id")
    if rec is None:
        if not sid:
            return None
        rec = SessionRecord(session_id=sid, transcript_path=path)

    kind = obj.get("type")
    if kind == "attachment":
        return rec
    if kind == "ai-title":
        rec.title = obj.get("aiTitle") or rec.title
        return rec
    if kind == "last-prompt":
        rec.last_prompt = obj.get("lastPrompt") or rec.last_prompt
        return rec

    if obj.get("cwd"):
        rec.project_path = obj["cwd"]
    if obj.get("gitBranch"):
        rec.git_branch = obj["gitBranch"]
    ts = obj.get("timestamp")
    if ts:
        if rec.started_at is None or ts < rec.started_at:
            rec.started_at = ts
        if rec.ended_at is None or ts > rec.ended_at:
            rec.ended_at = ts
    if obj.get("interruptedByShutdown") or obj.get("isAbortedMidStream"):
        rec.interrupted = True

    sidechain = bool(obj.get("isSidechain"))
    if kind == "user" and not sidechain:
        rec.user_msgs += 1
    elif kind == "assistant":
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if sidechain:
            rec.sidechain_tokens += int(usage.get("input_tokens") or 0) + int(
                usage.get("output_tokens") or 0
            )
            return rec
        rec.assistant_msgs += 1
        if msg.get("model"):
            rec.model = msg["model"]
        if obj.get("effort"):
            rec.effort = obj["effort"]
        rec.tokens_in += int(usage.get("input_tokens") or 0)
        rec.tokens_out += int(usage.get("output_tokens") or 0)
        rec.tokens_cache_create += int(usage.get("cache_creation_input_tokens") or 0)
        rec.tokens_cache_read += int(usage.get("cache_read_input_tokens") or 0)
    return rec
