"""The crown-jewel PII case table for the probe sanitizer."""

import pytest

from automator import sanitize


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    # os.path.expanduser reads HOME on POSIX; force a clean cache-free lookup
    return str(tmp_path)


# ------------------------------------------------------------- redact_home


def test_redact_home_replaces_home_prefix(home):
    assert sanitize.redact_home(f"{home}/.claude/x.jsonl") == "~/.claude/x.jsonl"


def test_redact_home_noop_when_absent(home):
    assert sanitize.redact_home("/etc/passwd") == "/etc/passwd"


# ------------------------------------------------------- looks_like_identifier


@pytest.mark.parametrize(
    "value",
    ["claude-opus-4-8", "session-abc_123", "Stop", "gpt-5-codex", "4.8", "abc123"],
)
def test_identifier_accepts_slugs(value):
    assert sanitize.looks_like_identifier(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "has spaces",
        "user@example.com",
        "/home/alice/x",
        "a/b",
        ".claude",  # leading dot is not alphanumeric
        "x" * 200,  # too long to be a slug
        "I am a sentence of prose.",
    ],
)
def test_identifier_rejects_prose_paths_emails(value):
    assert not sanitize.looks_like_identifier(value)


# --------------------------------------------------------------- scrub_json


def test_scrub_json_passes_numbers_bools_null():
    obj = {"input_tokens": 123, "ratio": 1.5, "ok": True, "off": False, "none": None}
    assert sanitize.scrub_json(obj) == obj


def test_scrub_json_keeps_keys_verbatim_redacts_string_leaves(home):
    obj = {
        "session_id": "abc-123",  # identifier -> kept
        "transcript_path": f"{home}/.claude/x.jsonl",  # path -> redacted
        "email": "me@example.com",  # email -> redacted
        "prose": "this is a free-form sentence",  # prose -> redacted
        "model": "claude-opus-4-8",  # identifier -> kept
    }
    out = sanitize.scrub_json(obj)
    assert set(out) == set(obj)  # keys kept verbatim
    assert out["session_id"] == "abc-123"
    assert out["model"] == "claude-opus-4-8"
    assert out["transcript_path"] == "<redacted:str>"
    assert out["email"] == "<redacted:str>"
    assert out["prose"] == "<redacted:str>"


def test_scrub_json_preserves_list_length_not_content():
    out = sanitize.scrub_json({"items": ["a b c", "tok-1", 7]})
    assert out["items"] == ["<redacted:str>", "tok-1", 7]


def test_scrub_json_depth_guard():
    obj = cur = {}
    for _ in range(60):
        cur["next"] = {}
        cur = cur["next"]
    cur["leaf"] = "deep"
    out = sanitize.scrub_json(obj, max_depth=10)
    # walk down to the guard
    node = out
    saw_guard = False
    for _ in range(60):
        if node == "<redacted:depth>":
            saw_guard = True
            break
        node = node.get("next")
        if node is None:
            break
    assert saw_guard


# --------------------------------------------------------------- scrub_text


def test_scrub_text_keeps_flags_redacts_email_and_home(home):
    text = f"Usage: foo [options]\n  --bar    do bar\ncontact me@example.com or see {home}/cfg"
    out = sanitize.scrub_text(text)
    assert "--bar" in out
    assert "me@example.com" not in out
    assert "<redacted:email>" in out
    assert f"{home}/cfg" not in out
    assert "~/cfg" in out


def test_scrub_text_max_lines_truncates():
    out = sanitize.scrub_text("\n".join(f"line{i}" for i in range(50)), max_lines=5)
    assert out.count("\n") == 5  # 5 kept lines + the ellipsis marker
    assert "more lines redacted" in out


def test_scrub_event_payload_is_scrub_json(home):
    payload = {"session_id": "s-1", "cwd": f"{home}/proj", "n": 5}
    out = sanitize.scrub_event_payload(payload)
    assert out == {"session_id": "s-1", "cwd": "<redacted:str>", "n": 5}
