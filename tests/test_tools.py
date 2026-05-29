"""Tests for the tool-call parser (tools.py).

A deterministic word-count stand-in for the tokenizer keeps the math exact and
keeps these tests off LiteLLM / the network.
"""
from __future__ import annotations

from tokview.tools import parse_completed_tool_calls


# Deterministic fake tokenizer: 1 token per whitespace-split word.
def wc(_model: str, text: str) -> int:
    return len(text.split())


# ---------- OpenAI shape ----------

def test_openai_completed_tool_call():
    messages = [
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "Read", "arguments": '{"path": "/etc/hosts"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "one two three four five"},
    ]
    out = parse_completed_tool_calls(messages, "gpt-4o", wc)
    assert len(out) == 1
    tc = out[0]
    assert tc["id"] == "call_1"
    assert tc["name"] == "Read"
    assert tc["result_tokens"] == 5            # five words
    assert tc["arg_tokens"] == wc("", '{"path": "/etc/hosts"}')


def test_openai_pending_call_skipped():
    """tool_use with no result yet must NOT be recorded."""
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}
        ]},
    ]
    assert parse_completed_tool_calls(messages, "gpt-4o", wc) == []


# ---------- Anthropic shape ----------

def test_anthropic_completed_tool_call():
    messages = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll read it"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "/x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "alpha beta gamma"},
        ]},
    ]
    out = parse_completed_tool_calls(messages, "claude-3-5-sonnet", wc)
    assert len(out) == 1
    assert out[0]["name"] == "Read"
    assert out[0]["result_tokens"] == 3


def test_anthropic_tool_result_list_content():
    """Anthropic tool_result content can be a list of text blocks."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "mcp__github__search", "input": {"q": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_2",
             "content": [{"type": "text", "text": "one two"}, {"type": "text", "text": "three"}]},
        ]},
    ]
    out = parse_completed_tool_calls(messages, "claude-3-5-sonnet", wc)
    assert len(out) == 1
    assert out[0]["name"] == "mcp__github__search"
    assert out[0]["result_tokens"] == 3  # "one two" + "three"


# ---------- mixed / multi ----------

def test_multiple_tools_in_one_conversation():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "a b c"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "d e"},
        ]},
    ]
    out = parse_completed_tool_calls(messages, "claude-3-5-sonnet", wc)
    by_name = {t["name"]: t for t in out}
    assert by_name["Read"]["result_tokens"] == 3
    assert by_name["Bash"]["result_tokens"] == 2


def test_empty_and_none():
    assert parse_completed_tool_calls(None, "gpt-4o", wc) == []
    assert parse_completed_tool_calls([], "gpt-4o", wc) == []
    assert parse_completed_tool_calls([{"role": "user", "content": "hi"}], "gpt-4o", wc) == []


def test_tokenizer_failure_falls_back_to_charcount():
    def boom(_m, _t):
        raise RuntimeError("tokenizer down")
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "function": {"name": "Read", "arguments": "x"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 40},
    ]
    out = parse_completed_tool_calls(messages, "gpt-4o", boom)
    assert out[0]["result_tokens"] == 10  # 40 chars // 4
