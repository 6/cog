"""Path boundary checks for cog._resolve — the security-critical guard that
prevents file tools from escaping the working directory."""
import os
import shutil
import tempfile
import unittest

import cog


class TestResolve(unittest.TestCase):
    def setUp(self):
        # Canonicalize to dodge /var vs /private/var on macOS.
        self.cwd = os.path.realpath(tempfile.mkdtemp())
        cog.tools_configure(cwd=self.cwd)

    def tearDown(self):
        shutil.rmtree(self.cwd, ignore_errors=True)

    def test_relative_path_joined_under_cwd(self):
        result = cog._resolve("foo.txt")
        self.assertEqual(result, os.path.join(self.cwd, "foo.txt"))

    def test_absolute_path_inside_cwd_ok(self):
        inside = os.path.join(self.cwd, "sub", "file.txt")
        self.assertEqual(cog._resolve(inside), inside)

    def test_cwd_itself_is_allowed(self):
        # list_dir(".") resolves to the cwd — must not raise.
        self.assertEqual(cog._resolve("."), self.cwd)

    def test_reject_parent_traversal(self):
        with self.assertRaises(ValueError):
            cog._resolve("../escape.txt")

    def test_reject_deep_traversal(self):
        with self.assertRaises(ValueError):
            cog._resolve("a/b/../../../escape.txt")

    def test_reject_absolute_outside_cwd(self):
        with self.assertRaises(ValueError):
            cog._resolve("/etc/passwd")

    def test_reject_co_named_sibling(self):
        # Regression: prior to the fix, a sibling directory whose path was a
        # prefix of the cwd (e.g., cwd="/tmp/abc", sibling="/tmp/abc2") would
        # pass the startswith check. The fix requires either exact match or a
        # trailing separator.
        sibling = self.cwd + "sneak"
        os.makedirs(sibling)
        try:
            target = os.path.join(sibling, "secret.txt")
            with self.assertRaises(ValueError):
                cog._resolve(target)
        finally:
            shutil.rmtree(sibling, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
