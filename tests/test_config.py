"""Config loading helpers: env-var expansion, walkup for local .cog/config.json,
skill file parsing."""

import os
import shutil
import tempfile
import unittest

import cog


class TestExpandEnv(unittest.TestCase):
    def setUp(self):
        os.environ["COG_TEST_VAR"] = "hello"
        os.environ.pop("COG_TEST_MISSING_XYZ", None)

    def tearDown(self):
        os.environ.pop("COG_TEST_VAR", None)

    def test_simple_string(self):
        self.assertEqual(cog._expand_env("${COG_TEST_VAR}"), "hello")

    def test_missing_becomes_empty(self):
        self.assertEqual(cog._expand_env("${COG_TEST_MISSING_XYZ}"), "")

    def test_embedded_in_string(self):
        self.assertEqual(cog._expand_env("x-${COG_TEST_VAR}-y"), "x-hello-y")

    def test_dict_recursive(self):
        self.assertEqual(
            cog._expand_env({"a": "${COG_TEST_VAR}", "b": 42}),
            {"a": "hello", "b": 42},
        )

    def test_list_recursive(self):
        self.assertEqual(
            cog._expand_env(["${COG_TEST_VAR}", "lit"]),
            ["hello", "lit"],
        )

    def test_non_string_passthrough(self):
        self.assertEqual(cog._expand_env(42), 42)
        self.assertEqual(cog._expand_env(None), None)
        self.assertEqual(cog._expand_env(True), True)


class TestFindLocalConfig(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_config(self, dirpath):
        cog_dir = os.path.join(dirpath, ".cog")
        os.makedirs(cog_dir, exist_ok=True)
        path = os.path.join(cog_dir, "config.json")
        with open(path, "w") as f:
            f.write("{}")
        return path

    def test_direct_match(self):
        expected = self._write_config(self.root)
        self.assertEqual(cog._find_local_config(self.root), expected)

    def test_walkup_from_subdir(self):
        expected = self._write_config(self.root)
        sub = os.path.join(self.root, "a", "b", "c")
        os.makedirs(sub)
        self.assertEqual(cog._find_local_config(sub), expected)

    def test_nearest_wins(self):
        self._write_config(self.root)
        inner = os.path.join(self.root, "sub")
        expected_inner = self._write_config(inner)
        deeper = os.path.join(inner, "x", "y")
        os.makedirs(deeper)
        self.assertEqual(cog._find_local_config(deeper), expected_inner)


if __name__ == "__main__":
    unittest.main()
