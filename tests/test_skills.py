"""Skill loader — walks skills_dirs for SKILL.md, parses optional YAML-ish
frontmatter, returns {name, text} entries."""

import os
import shutil
import tempfile
import unittest

import cog


class TestLoadSkills(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk_skill(self, name, body):
        sdir = os.path.join(self.root, name)
        os.makedirs(sdir)
        with open(os.path.join(sdir, "SKILL.md"), "w") as f:
            f.write(body)

    def test_no_frontmatter_uses_dirname(self):
        self._mk_skill("hello", "this is hello")
        skills = cog._load_skills([self.root])
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "hello")
        self.assertEqual(skills[0]["text"], "this is hello")

    def test_frontmatter_name_override(self):
        self._mk_skill("raw-dir", "---\nname: custom\ndescription: x\n---\nbody here\n")
        skills = cog._load_skills([self.root])
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["name"], "custom")
        self.assertEqual(skills[0]["text"], "body here")

    def test_empty_body_skipped(self):
        self._mk_skill("empty", "---\nname: x\n---\n\n")
        skills = cog._load_skills([self.root])
        self.assertEqual(skills, [])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(cog._load_skills(["/nonexistent/path/xyz"]), [])

    def test_multiple_skills(self):
        self._mk_skill("a", "A body")
        self._mk_skill("b", "---\nname: bee\n---\nB body")
        skills = cog._load_skills([self.root])
        names = sorted(s["name"] for s in skills)
        self.assertEqual(names, ["a", "bee"])


if __name__ == "__main__":
    unittest.main()
