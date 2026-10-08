"""Literal launch tests: widening execution syntax must be an explicit change."""

import unittest

from policy import (
    IntegrityError,
    literal_argv,
    validate_absolute_path,
    validate_command,
    validate_entrypoint,
)


def invocation(command="/usr/bin/python3.11 /project/run.py", **overrides):
    value = {
        "tool": "Bash", "command": command, "workdir": "/project",
        "shell": "/usr/bin/bash", "login": False, "tty": False,
    }
    return value | overrides


class PolicyTests(unittest.TestCase):
    def test_supported_interpreters_return_script_without_suffix_classification(self):
        for executable in ("/usr/bin/python3.11", "/usr/bin/bash"):
            with self.subTest(executable=executable):
                self.assertEqual(
                    validate_command(invocation(f"{executable} /project/no-suffix --flag value")),
                    "/project/no-suffix",
                )

    def test_space_quoting_is_literal_and_command_text_is_preserved(self):
        command = '/usr/bin/python3.11 "/project/two words" \'one two\' ""'
        value = invocation(command)
        self.assertEqual(literal_argv(value), ["/usr/bin/python3.11", "/project/two words", "one two", ""])
        self.assertEqual(value["command"], command)

    def test_only_exact_supported_launch_forms_are_accepted(self):
        commands = [
            "/usr/bin/env /usr/bin/python3.11 /project/run.py",
            "/tmp/python3 /project/run.py",
            "/usr/bin/find /project -exec /usr/bin/python3.11 /project/run.py +",
            "/usr/bin/busybox sh /project/run.py",
            "/usr/bin/python3.11 -c literal",
            "/usr/bin/python3.11 -m package",
            "/usr/bin/python3.11 -",
            "/usr/bin/python3.11 -- /project/run.py",
            "/usr/bin/bash -c literal",
            "/usr/bin/bash", "/usr/bin/true", "/project/run.py",
            "/usr/bin/python3 /project/run.py",
            "python3.11 /project/run.py",
            "/usr/bin/python3.11 relative.py",
        ]
        for command in commands:
            with self.subTest(command=command):
                with self.assertRaises(IntegrityError):
                    validate_command(invocation(command))

    def test_pinning_wrapper_cannot_make_unsupported_launch_valid(self):
        value = invocation("/usr/bin/env /usr/bin/python3.11 /project/run.py")
        with self.assertRaises(IntegrityError):
            validate_entrypoint(value, {"files": [{"path": "/usr/bin/env"}, {"path": "/project/run.py"}]})

    def test_project_locations_never_bypass_launch_policy(self):
        for workdir in ("/project-one", "/project-two", "/tmp/worktree-one", "/tmp/worktree-two"):
            with self.subTest(workdir=workdir):
                self.assertEqual(validate_command(invocation(workdir=workdir)), "/project/run.py")
                with self.assertRaises(IntegrityError):
                    validate_command(invocation("/usr/bin/rg --pre /project/filter", workdir=workdir))

    def test_script_entrypoint_must_be_in_snapshot(self):
        validate_entrypoint(invocation(), {"files": [{"path": "/project/run.py"}]})
        with self.assertRaises(IntegrityError):
            validate_entrypoint(invocation(), {"files": [{"path": "/project/other.py"}]})

    def test_shell_expansion_and_operators_are_rejected_even_when_quoted(self):
        for character in ";&|<>$`\\*?[]{}()~#\n\r\x00":
            with self.subTest(character=repr(character)):
                with self.assertRaises(IntegrityError):
                    validate_command(invocation(f"/usr/bin/python3.11 /project/run.py '{character}'"))
        for command in ("", " ", "'unterminated", "A=value /usr/bin/bash /project/run.sh"):
            with self.subTest(command=command):
                with self.assertRaises(IntegrityError):
                    validate_command(invocation(command))

    def test_nonliteral_context_is_rejected(self):
        for overrides in ({"tool": "Other"}, {"login": True}, {"login": 0}, {"tty": True}, {"tty": 0},
                          {"workdir": "relative"}, {"shell": "/usr//bin/bash"}, {"command": None}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(IntegrityError):
                    literal_argv(invocation(**overrides))

    def test_paths_have_one_absolute_spelling(self):
        for valid in ("/", "/project/file", "/project/two words"):
            validate_absolute_path(valid)
        for invalid in (None, 1, "", "relative", "//project", "/project/", "/project//file", "/project/./file", "/project/../file", "/project/\x00file"):
            with self.subTest(path=invalid):
                with self.assertRaises(IntegrityError):
                    validate_absolute_path(invalid)
        for script in ("/project/./run.py", "/project//run.py", "/project/run.py/"):
            with self.subTest(script=script):
                with self.assertRaises(IntegrityError):
                    validate_command(invocation(f"/usr/bin/bash {script}"))


if __name__ == "__main__":
    unittest.main()
