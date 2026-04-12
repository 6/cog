"""parse_sse_stream — the Anthropic Messages API SSE wire-format decoder.
Highest-leverage test target: a regression in the parser silently breaks every
turn. We feed it synthetic byte streams and assert the yielded event shape."""
import json
import unittest

import cog


class _FakeResponse:
    """Matches the readline() interface of http.client.HTTPResponse."""

    def __init__(self, data: bytes):
        self._lines = data.splitlines(keepends=True)
        self._i = 0

    def readline(self):
        if self._i >= len(self._lines):
            return b""
        line = self._lines[self._i]
        self._i += 1
        return line


def _encode(events):
    out = b""
    for event_type, data in events:
        out += f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()
    return out


class TestParseSseStream(unittest.TestCase):
    def test_plain_text_turn(self):
        resp = _FakeResponse(_encode([
            ("message_start", {"message": {"usage": {"input_tokens": 10}}}),
            ("content_block_start", {"index": 0, "content_block": {"type": "text"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
            ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": " world"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
            ("message_stop", {}),
        ]))
        events = list(cog.parse_sse_stream(resp))
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("text_delta"), 2)
        self.assertEqual(kinds.count("text_final"), 1)
        self.assertEqual(kinds.count("block_done"), 1)
        self.assertEqual(kinds.count("stop"), 1)
        self.assertEqual(kinds.count("usage"), 1)

        deltas = [p for k, p in events if k == "text_delta"]
        self.assertEqual("".join(deltas), "Hello world")
        finals = [p for k, p in events if k == "text_final"]
        self.assertEqual(finals, ["Hello world"])

        (_, usage) = next(e for e in events if e[0] == "usage")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 5)

    def test_tool_use_accumulates_partial_json(self):
        resp = _FakeResponse(_encode([
            ("message_start", {"message": {"usage": {"input_tokens": 5}}}),
            ("content_block_start", {"index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"}}),
            ("content_block_delta", {"index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path": "'}}),
            ("content_block_delta", {"index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 'x.txt"}'}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 10}}),
            ("message_stop", {}),
        ]))
        events = list(cog.parse_sse_stream(resp))
        tool_uses = [p for k, p in events if k == "tool_use"]
        self.assertEqual(len(tool_uses), 1)
        self.assertEqual(tool_uses[0]["id"], "t1")
        self.assertEqual(tool_uses[0]["name"], "read_file")
        self.assertEqual(tool_uses[0]["input"], {"path": "x.txt"})

        # block_done for the tool_use should precede tool_use (ordering matters —
        # the agent loop relies on block_done to build the assistant message).
        kinds = [k for k, _ in events]
        bd_idx = kinds.index("block_done")
        tu_idx = kinds.index("tool_use")
        self.assertLess(bd_idx, tu_idx)

    def test_malformed_tool_json_returns_raw(self):
        resp = _FakeResponse(_encode([
            ("message_start", {"message": {"usage": {"input_tokens": 1}}}),
            ("content_block_start", {"index": 0,
                "content_block": {"type": "tool_use", "id": "t2", "name": "x"}}),
            ("content_block_delta", {"index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{bad"}}),
            ("content_block_stop", {"index": 0}),
            ("message_delta", {"delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 1}}),
            ("message_stop", {}),
        ]))
        events = list(cog.parse_sse_stream(resp))
        tool_uses = [p for k, p in events if k == "tool_use"]
        self.assertEqual(tool_uses[0]["input"], {"_raw": "{bad"})

    def test_empty_stream_terminates(self):
        resp = _FakeResponse(b"")
        events = list(cog.parse_sse_stream(resp))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
