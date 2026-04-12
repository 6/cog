"""Tool implementations: read_file, write_file, str_replace, list_dir. Each
tool returns either an OK string or an `ERROR: ...` string; we never rely on
exceptions escaping into the agent loop."""
import os
import shutil
import tempfile
import unittest

import cog


class TestTools(unittest.TestCase):
    def setUp(self):
        self.cwd = os.path.realpath(tempfile.mkdtemp())
        cog.tools_configure(cwd=self.cwd)

    def tearDown(self):
        shutil.rmtree(self.cwd, ignore_errors=True)

    # read_file ---------------------------------------------------------

    def test_read_file_ok(self):
        path = os.path.join(self.cwd, "a.txt")
        with open(path, "w") as f:
            f.write("hello")
        self.assertEqual(cog.tool_read_file({"path": "a.txt"}), "hello")

    def test_read_file_missing(self):
        r = cog.tool_read_file({"path": "nope.txt"})
        self.assertTrue(r.startswith("ERROR:"))
        self.assertIn("not found", r)

    def test_read_file_binary_rejected(self):
        path = os.path.join(self.cwd, "bin")
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02")
        r = cog.tool_read_file({"path": "bin"})
        self.assertTrue(r.startswith("ERROR:"))
        self.assertIn("binary", r)

    def test_read_file_outside_cwd_rejected(self):
        r = cog.tool_read_file({"path": "/etc/passwd"})
        self.assertTrue(r.startswith("ERROR:"))

    # write_file --------------------------------------------------------

    def test_write_file_ok(self):
        r = cog.tool_write_file({"path": "x.txt", "content": "hi"})
        self.assertTrue(r.startswith("OK:"))
        with open(os.path.join(self.cwd, "x.txt")) as f:
            self.assertEqual(f.read(), "hi")

    def test_write_file_creates_parents(self):
        r = cog.tool_write_file({"path": "a/b/c.txt", "content": "deep"})
        self.assertTrue(r.startswith("OK:"))
        self.assertTrue(os.path.exists(os.path.join(self.cwd, "a/b/c.txt")))

    def test_write_file_outside_cwd_rejected(self):
        r = cog.tool_write_file({"path": "/tmp/cog-escape.txt", "content": "x"})
        self.assertTrue(r.startswith("ERROR:"))

    # str_replace -------------------------------------------------------

    def test_str_replace_unique(self):
        path = os.path.join(self.cwd, "f.txt")
        with open(path, "w") as f:
            f.write("alpha beta gamma")
        r = cog.tool_str_replace({"path": "f.txt", "old_str": "beta", "new_str": "nim"})
        self.assertTrue(r.startswith("OK:"))
        with open(path) as f:
            self.assertEqual(f.read(), "alpha nim gamma")

    def test_str_replace_not_found(self):
        path = os.path.join(self.cwd, "f.txt")
        with open(path, "w") as f:
            f.write("alpha")
        r = cog.tool_str_replace({"path": "f.txt", "old_str": "zz", "new_str": "y"})
        self.assertTrue(r.startswith("ERROR:"))
        self.assertIn("not found", r)

    def test_str_replace_ambiguous(self):
        path = os.path.join(self.cwd, "f.txt")
        with open(path, "w") as f:
            f.write("a\na\na")
        r = cog.tool_str_replace({"path": "f.txt", "old_str": "a", "new_str": "b"})
        self.assertTrue(r.startswith("ERROR:"))
        self.assertIn("matched", r)

    # list_dir ----------------------------------------------------------

    def test_list_dir_marks_types(self):
        os.makedirs(os.path.join(self.cwd, "sub"))
        with open(os.path.join(self.cwd, "a.txt"), "w") as f:
            f.write("x")
        r = cog.tool_list_dir({"path": "."})
        self.assertIn("[file]", r)
        self.assertIn("[dir] ", r)
        self.assertIn("a.txt", r)
        self.assertIn("sub", r)

    def test_list_dir_empty(self):
        r = cog.tool_list_dir({"path": "."})
        self.assertEqual(r, "(empty directory)")

    def test_list_dir_missing(self):
        r = cog.tool_list_dir({"path": "nope"})
        self.assertTrue(r.startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
