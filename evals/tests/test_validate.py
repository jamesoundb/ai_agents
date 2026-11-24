#!/usr/bin/env python3
"""validate.py checks the case format `claude plugin eval` actually loads (CLI 2.1.331).

Each test builds a throwaway case and asserts validate.py reports what the real CLI does with it:
an error where the CLI refuses to load or run the case, nothing where it accepts it.
Run: python3 evals/tests/test_validate.py
"""
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import validate  # noqa: E402


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(text).lstrip())
    if mode:
        os.chmod(path, mode)


class CaseFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.case = os.path.join(self.tmp.name, "a-case")
        validate.errors.clear()
        validate.warnings.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def prompt(self, frontmatter):
        p = os.path.join(self.case, "prompt.md")
        write(p, f"---\n{frontmatter}\n---\nDo the thing.\n")
        validate.check_prompt("a-case", p)

    def grader(self, frontmatter):
        p = os.path.join(self.case, "graders", "1.md")
        write(p, f"---\n{frontmatter}\n---\nwhy\n")
        validate.check_grader("a-case", p)

    def case_yaml(self, context):
        write(os.path.join(self.case, "scaffold.sh"), "#!/usr/bin/env bash\n", 0o755)
        write(os.path.join(self.case, "fix", "f.txt"), "x\n")
        p = os.path.join(self.case, "case.yaml")
        write(p, f'schema_version: "1.1"\nname: "a case"\ncontext:\n{context}\n')
        validate.check_case_yaml("a-case", p)

    def assertError(self, fragment):
        self.assertTrue(any(fragment in e for e in validate.errors),
                        f"no error containing {fragment!r}; errors: {validate.errors}")

    def assertClean(self):
        self.assertEqual(validate.errors, [])

    # prompt.md: "unknown frontmatter key "agent"" -- the case does not load.
    def test_agent_key_in_prompt_is_an_error(self):
        self.prompt("name: x\nagent: helm")
        self.assertError("agent")

    def test_unknown_prompt_key_is_an_error(self):
        self.prompt("name: x\nfresh_home: true")
        self.assertError("fresh_home")

    def test_documented_prompt_keys_are_clean(self):
        self.prompt('name: x\nruns: 3\nmax_turns: 10\nallowed_tools: ["Bash"]\nexpected_outcome: pass')
        self.assertClean()

    # graders: JavaScript regex -- "Invalid regular expression: invalid regular expression modifier".
    def test_inline_flags_are_an_error(self):
        for pattern in ("(?is)evidence.{0,40}limits", "(?i)rerun", "a(?s)b"):
            validate.errors.clear()
            self.grader(f"type: regex\npattern: '{pattern}'")
            self.assertError("inline flag")

    def test_flags_field_and_lookaheads_are_clean(self):
        self.grader("type: regex\npattern: '(?=.*a)(?=.*b)'\nflags: \"is\"")
        self.assertClean()

    def test_unknown_flag_is_an_error(self):
        self.grader("type: regex\npattern: 'a'\nflags: \"ix\"")
        self.assertError("flags")

    def test_python_only_syntax_is_an_error(self):
        self.grader("type: regex\npattern: '(?P<x>a)\\Z'")
        self.assertError("JavaScript")

    # case.yaml: 'path "../scaffold/prepare.sh" escapes the case directory'.
    def test_paths_outside_the_case_dir_are_an_error(self):
        self.case_yaml('  scaffold_script: "../scaffold.sh"')
        self.assertError("escapes the case directory")
        validate.errors.clear()
        self.case_yaml('  scaffold_script: "scaffold.sh"\n  add_dirs: ["../fix/"]')
        self.assertError("escapes the case directory")

    def test_paths_inside_the_case_dir_are_clean(self):
        self.case_yaml('  scaffold_script: "scaffold.sh"\n  add_dirs: ["fix/"]')
        self.assertClean()

    def test_agent_must_exist(self):
        self.case_yaml('  scaffold_script: "scaffold.sh"\n  agent: no-such-agent')
        self.assertError("no-such-agent")
        validate.errors.clear()
        self.case_yaml('  scaffold_script: "scaffold.sh"\n  agent: helm')
        self.assertClean()

    def test_harnesses_must_be_known(self):
        self.case_yaml('  scaffold_script: "scaffold.sh"\n  harnesses: [antigravity, vim]')
        self.assertError("vim")


class Suite(unittest.TestCase):
    def test_the_suite_validates(self):
        validate.errors.clear()
        validate.warnings.clear()
        self.assertEqual(validate.main(), 0, validate.errors)


if __name__ == "__main__":
    unittest.main()
