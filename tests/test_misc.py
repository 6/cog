"""Small pure-function tests: _summarize_args, _mcp_parse_www_auth."""
import unittest

import cog


class TestSummarizeArgs(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(cog._summarize_args({}), "")

    def test_single_short(self):
        self.assertEqual(cog._summarize_args({"path": "f.txt"}), 'path="f.txt"')

    def test_long_value_truncated(self):
        r = cog._summarize_args({"x": "a" * 200})
        self.assertIn("...", r)
        # Per-value truncation is ~80 chars (with ellipsis).
        self.assertLess(len(r), 200)

    def test_total_length_capped(self):
        r = cog._summarize_args({"a": "1" * 30, "b": "2" * 30, "c": "3" * 30}, max_len=50)
        self.assertLessEqual(len(r), 50)

    def test_non_string_value(self):
        r = cog._summarize_args({"n": 42, "b": True})
        self.assertIn("42", r)
        self.assertIn("True", r)


class TestMcpParseWwwAuth(unittest.TestCase):
    def test_quoted_resource_metadata(self):
        h = 'Bearer resource_metadata="https://example.com/meta"'
        self.assertEqual(cog._mcp_parse_www_auth(h), "https://example.com/meta")

    def test_no_resource_metadata(self):
        self.assertIsNone(cog._mcp_parse_www_auth("Bearer realm=foo"))

    def test_mixed_params(self):
        h = 'Bearer realm="test", resource_metadata="https://x/m", error="invalid"'
        self.assertEqual(cog._mcp_parse_www_auth(h), "https://x/m")

    def test_empty_header(self):
        self.assertIsNone(cog._mcp_parse_www_auth(""))


if __name__ == "__main__":
    unittest.main()
