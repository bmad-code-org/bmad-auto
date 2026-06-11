import json

from automator.tokens import read_usage, tally, tally_codex_rollout, tally_gemini_chat


def test_tally_mixed_shapes(tmp_path):
    lines = [
        # Claude Code shape: usage nested in message
        {"type": "assistant", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}},
        # cache fields
        {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 300,
                }
            },
        },
        # top-level usage shape
        {"type": "message", "usage": {"input_tokens": 1, "output_tokens": 1}},
        # noise: no usage, malformed values tolerated
        {"type": "user", "message": {"content": "hi"}},
        {"type": "summary"},
    ]
    path = tmp_path / "t.jsonl"
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
        f.write("not json at all\n")
        f.write("\n")

    usage = tally(path)
    assert usage.input_tokens == 111
    assert usage.output_tokens == 56
    assert usage.cache_read_tokens == 2000
    assert usage.cache_creation_tokens == 300
    assert usage.total == 111 + 56 + 2000 + 300


def test_tally_missing_file(tmp_path):
    assert tally(tmp_path / "nope.jsonl").total == 0


def test_codex_rollout_last_cumulative_wins(tmp_path):
    lines = [
        {"type": "session_meta", "payload": {"id": "abc"}},
        # token_count payloads are cumulative; only the last one counts
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 10,
                    }
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "agent_message"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 500,
                        "cached_input_tokens": 200,
                        "output_tokens": 60,
                    }
                },
            },
        },
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")

    usage = tally_codex_rollout(path)
    assert usage.input_tokens == 300  # cached portion split out of input
    assert usage.cache_read_tokens == 200
    assert usage.output_tokens == 60


def test_codex_rollout_without_token_counts_is_none(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "agent_message"}}) + "\n")
    assert tally_codex_rollout(path) is None
    assert tally_codex_rollout(tmp_path / "nope.jsonl") is None


def test_gemini_chat_dedupes_reemitted_messages(tmp_path):
    # shape captured from a real ~/.gemini/tmp/<project>/chats/session-*.jsonl
    # (2026-06-11): a JSONL patch stream where the same message id is
    # re-emitted as it accretes content, and `input` includes `cached`.
    lines = [
        {"sessionId": "s1", "projectHash": "x", "kind": "main"},
        {"$set": {"messages": [{"id": "u1", "type": "user", "content": []}]}},
        {
            "id": "g1",
            "type": "gemini",
            "tokens": {"input": 12273, "output": 45, "cached": 0, "thoughts": 87, "tool": 0},
        },
        {"$set": {"lastUpdated": "..."}},
        # same message re-emitted with toolCalls added: must not double-count
        {
            "id": "g1",
            "type": "gemini",
            "toolCalls": [{}],
            "tokens": {"input": 12273, "output": 45, "cached": 0, "thoughts": 87, "tool": 0},
        },
        {
            "id": "g2",
            "type": "gemini",
            "tokens": {"input": 12429, "output": 2, "cached": 11367, "thoughts": 16, "tool": 0},
        },
    ]
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")

    usage = tally_gemini_chat(path)
    assert usage.input_tokens == 12273 + (12429 - 11367)  # cached split out of input
    assert usage.cache_read_tokens == 11367
    assert usage.output_tokens == (45 + 87) + (2 + 16)  # output + thoughts


def test_gemini_chat_without_tokens_is_none(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"id": "u1", "type": "user", "content": []}) + "\n")
    assert tally_gemini_chat(path) is None
    assert tally_gemini_chat(tmp_path / "nope.jsonl") is None


def test_read_usage_dispatch(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"usage": {"input_tokens": 1, "output_tokens": 2}}) + "\n")
    assert read_usage("claude-jsonl", path).total == 3
    assert read_usage("none", path) is None
