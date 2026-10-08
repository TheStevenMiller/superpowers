"""Synthetic host adapters exist only here; checker/store/protocol remain real."""

from contextlib import ExitStack
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import hook
from manifest import canonical_bytes, load_json
from policy import IntegrityError, literal_argv
from prepare import prepare_request
from store import approve, approved_candidate, revoke


def synthetic_normalize(event):
    """Hypothetical full-context contract, never a production host adapter."""
    invocation = {"tool": event["tool_name"], **event["tool_input"]}
    if set(invocation) != {"tool", "command", "workdir", "shell", "login", "tty"}:
        raise IntegrityError("synthetic context incomplete")
    if any(not isinstance(invocation[key], str) for key in ("tool", "command", "workdir", "shell")):
        raise IntegrityError("synthetic context types invalid")
    if any(type(invocation[key]) is not bool for key in ("login", "tty")):
        raise IntegrityError("synthetic context types invalid")
    literal_argv(invocation)
    return invocation


def synthetic_prompt_delivery(event):
    """Test-only assertion; no event flag enables this in production."""
    if event.get("test_human_delivery") is not True:
        raise IntegrityError("synthetic delivery unavailable")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.state = self.base / "state"
        self.marker = self.base / "executed"
        self.script = self.base / "run.py"
        self.script.write_text(f"from pathlib import Path\nPath({str(self.marker)!r}).touch()\n")
        self.invocation = {"tool": "Bash", "command": f"/usr/bin/python3.11 {self.script}",
                           "workdir": str(self.base), "shell": "/usr/bin/bash", "login": False, "tty": False}
        self.request = self.base / "request.json"
        self.request.write_bytes(canonical_bytes({"invocation": self.invocation, "roots": [str(self.script)]}))
        self.prepared = prepare_request(self.request, self.state)
        self.digest = self.prepared["digest"]
        self.release = self.base / "synthetic-release-v1"
        self.release.mkdir()
        checksums = {}
        for name in ("policy.py", "manifest.py", "store.py", "prepare.py", "hook.py", "configure.py"):
            raw = f"# synthetic release fixture: {name}; never executed\n".encode()
            (self.release / name).write_bytes(raw)
            checksums[name] = hashlib.sha256(raw).hexdigest()
        self.metadata = self.release / "release-metadata.json"
        self.release_info = {"python_path": "/usr/bin/python3.11", "hook_path": str(self.release / "hook.py"),
                             "prepare_path": str(self.release / "prepare.py"), "checksums": checksums}
        self.metadata.write_bytes(canonical_bytes(self.release_info))

    def tearDown(self):
        self.assertFalse(self.marker.exists(), "the protected command must never execute")

    def execution_event(self, invocation=None):
        invocation = invocation or self.invocation
        return {"hook_event_name": "PreToolUse", "tool_name": invocation["tool"],
                "session_id": "synthetic-session", "cwd": "/wrong-session-cwd",
                "tool_input": {key: value for key, value in invocation.items() if key != "tool"}}

    def prompt_event(self, prompt=None):
        return {"hook_event_name": "UserPromptSubmit", "session_id": "synthetic-session",
                "cwd": "/wrong-session-cwd", "test_human_delivery": True,
                "prompt": prompt if prompt is not None else f"approve-files sha256:{self.digest}"}

    def receipts(self):
        return {path.name: path.read_bytes() for path in (self.state / "receipts").iterdir()}

    def assert_denied(self, response, event_name="PreToolUse"):
        self.assertIsInstance(response, dict)
        if event_name == "PreToolUse":
            self.assertIn("hookSpecificOutput", response)
            output = response["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], event_name)
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertNotIn("decision", response)
        else:
            self.assertEqual(response.get("decision"), "block")
            self.assertNotIn("hookSpecificOutput", response)

    def call_event(self, event, *, deadline=None, synthetic=True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(hook, "RELEASE_METADATA_PATH", self.metadata))
            if synthetic:
                stack.enter_context(patch.object(hook, "normalize_invocation", synthetic_normalize))
                stack.enter_context(patch.object(hook, "require_supported_prompt_delivery", synthetic_prompt_delivery))
            return hook.handle_event(event, self.state, deadline=deadline)

    def test_production_normalizer_and_prompt_delivery_are_unconditionally_unsupported(self):
        for event in (self.execution_event(), {}, self.execution_event() | {"supported": True, "synthetic": True}):
            with self.subTest(event=event), self.assertRaises(IntegrityError):
                hook.normalize_invocation(event)
        with self.assertRaises(IntegrityError):
            hook.require_supported_prompt_delivery(self.prompt_event())

    def test_production_events_cannot_pass_or_create_or_revoke_receipts(self):
        before = self.receipts()
        self.assert_denied(self.call_event(self.execution_event(), synthetic=False))
        self.assert_denied(self.call_event(self.prompt_event(), synthetic=False), "UserPromptSubmit")
        self.assertEqual(self.receipts(), before)
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        before = self.receipts()
        self.assert_denied(self.call_event(self.prompt_event(f"revoke-files sha256:{self.digest}"), synthetic=False), "UserPromptSubmit")
        self.assertEqual(self.receipts(), before)

    def test_corrupt_store_is_explicit_denial(self):
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        (self.state / "receipts" / f"{self.digest}.json").write_bytes(b"{ corrupt private detail")
        response = self.call_event(self.execution_event())
        self.assert_denied(response)
        self.assertNotIn("private detail", json.dumps(response))

    def test_pass_preserves_host_permission_flow_and_freshly_hashes(self):
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        before = self.receipts()
        self.assertEqual(self.call_event(self.execution_event()), {})
        previous = self.script.stat()
        self.script.write_text("changed input\n")
        os.utime(self.script, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assert_denied(self.call_event(self.execution_event()))
        self.assertEqual(self.receipts(), before)

    def test_unapproved_and_revoked_baselines_deny(self):
        self.assert_denied(self.call_event(self.execution_event()))
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        revoke(self.state, self.digest, "synthetic-direct-store-setup")
        self.assert_denied(self.call_event(self.execution_event()))

    def test_offline_lifecycle_requires_fresh_explicit_approval_after_drift(self):
        event = self.execution_event()
        self.assertEqual(self.receipts(), {})
        self.assert_denied(self.call_event(event))
        response = self.call_event(self.prompt_event())
        self.assertEqual(response["hookSpecificOutput"]["additionalContext"],
                         "Files approved; execution permission is unchanged.")
        receipts_before = self.receipts()
        candidate_path = self.state / "candidates" / f"{self.digest}.json"
        candidate_before = candidate_path.read_bytes()
        self.assertEqual(self.call_event(event), {})

        previous = self.script.stat()
        self.script.write_bytes(self.script.read_bytes() + b"# changed declared input\n")
        os.utime(self.script, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assertEqual(self.script.stat().st_mtime_ns, previous.st_mtime_ns)
        self.assert_denied(self.call_event(event))
        self.assertEqual(candidate_path.read_bytes(), candidate_before)
        self.assertEqual(self.receipts(), receipts_before)

        fresh = prepare_request(self.request, self.state)
        self.assertNotEqual(fresh["digest"], self.digest)
        self.assertEqual(self.receipts(), receipts_before)
        self.assert_denied(self.call_event(event), "PreToolUse")
        response = self.call_event(self.prompt_event(f"approve-files sha256:{fresh['digest']}"))
        self.assertEqual(response["hookSpecificOutput"]["additionalContext"],
                         "Files approved; execution permission is unchanged.")
        self.assertEqual(self.call_event(event), {})
        old_receipt = self.state / "receipts" / f"{self.digest}.json"
        self.assertIs(load_json(old_receipt.read_bytes())["active"], False)
        response = self.call_event(self.prompt_event(f"revoke-files sha256:{fresh['digest']}"))
        self.assertEqual(response["hookSpecificOutput"]["additionalContext"],
                         "Files revoked; execution permission is unchanged.")
        self.assert_denied(self.call_event(event))

    def assert_workdirs_have_independent_approvals(self, projects):
        invocations, prepared = [], []
        for project in projects:
            invocation = self.invocation | {"workdir": str(project)}
            invocations.append(invocation)
            request = project / "request.json"
            request.write_bytes(canonical_bytes({"invocation": invocation,
                                                "roots": [str(self.script), str(project / "inputs")]}))
            prepared.append(prepare_request(request, self.state))
        self.assertEqual(invocations[0]["command"], invocations[1]["command"])
        self.assertNotEqual(prepared[0]["digest"], prepared[1]["digest"])
        self.call_event(self.prompt_event(f"approve-files sha256:{prepared[0]['digest']}"))
        self.assertEqual(self.call_event(self.execution_event(invocations[0])), {})
        self.assert_denied(self.call_event(self.execution_event(invocations[1])))
        self.assertFalse((self.state / "receipts" / f"{prepared[1]['digest']}.json").exists())
        self.call_event(self.prompt_event(f"approve-files sha256:{prepared[1]['digest']}"))
        for invocation in invocations:
            self.assertEqual(self.call_event(self.execution_event(invocation)), {})

    def test_independent_projects_cannot_reuse_identical_command_approval(self):
        projects = [self.base / "project-one", self.base / "project-two"]
        for project in projects:
            (project / "inputs").mkdir(parents=True)
            (project / "inputs" / "settings.json").write_bytes(b'{"synthetic":true}\n')
        self.assert_workdirs_have_independent_approvals(projects)

    def test_linked_git_worktrees_cannot_reuse_identical_command_approval(self):
        repository = self.base / "git-fixture"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}

        def git(*args):
            return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                                   "-c", "user.name=Offline fixture", "-c", "user.email=offline@example.invalid",
                                   *args], env=environment, check=True, capture_output=True, text=True, timeout=5)

        git("init", "--quiet", "--initial-branch=main", str(repository))
        (repository / "inputs").mkdir()
        (repository / "inputs" / "settings.json").write_bytes(b'{"synthetic":true}\n')
        git("-C", str(repository), "add", "--", "inputs")
        git("-C", str(repository), "commit", "--quiet", "-m", "Offline worktree fixture")
        projects = [self.base / "worktree-one", self.base / "worktree-two"]
        for project in projects:
            git("-C", str(repository), "worktree", "add", "--quiet", "--detach", str(project), "HEAD")
            self.assertTrue((project / ".git").is_file())
            common = git("-C", str(project), "rev-parse", "--git-common-dir").stdout.strip()
            self.assertEqual((project / common).resolve(), repository / ".git")
        self.assert_workdirs_have_independent_approvals(projects)

    def test_declared_directory_addition_denies_but_undeclared_changes_are_not_attested(self):
        inputs = self.base / "inputs"
        inputs.mkdir()
        config = inputs / "settings.json"
        config.write_bytes(b'{"synthetic":true}\n')
        outside = self.base / "undeclared.txt"
        outside.write_text("outside the approval scope\n")
        self.request.write_bytes(canonical_bytes({"invocation": self.invocation,
                                                 "roots": [str(self.script), str(inputs)]}))
        prepared = prepare_request(self.request, self.state)
        self.assertEqual({item["path"] for item in prepared["manifest"]["files"]},
                         {str(self.script), str(config)})
        self.call_event(self.prompt_event(f"approve-files sha256:{prepared['digest']}"))
        before = self.receipts()
        self.assertEqual(self.call_event(self.execution_event()), {})
        outside.write_text("changed, still outside the approval scope\n")
        self.assertEqual(self.call_event(self.execution_event()), {})
        self.assertEqual(self.receipts(), before)
        (inputs / ".new-input").write_text("new declared-directory member\n")
        self.assert_denied(self.call_event(self.execution_event()))
        self.assertEqual(self.receipts(), before)

    def test_changed_flags_workdir_or_missing_effective_context_deny(self):
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        for change in ({"workdir": "/other-project"}, {"command": self.invocation["command"] + " extra"},
                       {"shell": "/other/bash"}, {"login": True}, {"tty": True}):
            with self.subTest(change=change):
                self.assert_denied(self.call_event(self.execution_event(self.invocation | change)))
        for field in ("workdir", "shell", "login", "tty"):
            event = self.execution_event()
            del event["tool_input"][field]
            with self.subTest(missing=field):
                self.assert_denied(self.call_event(event))

    def test_unsupported_launch_syntax_is_never_approved_by_the_hook(self):
        for command in ("/usr/bin/rg --pre /project/filter", "/usr/bin/bash", "/usr/bin/python3.11 -c pass",
                        self.invocation["command"] + " > /tmp/output", self.invocation["command"] + " $(true)",
                        self.invocation["command"] + " *", self.invocation["command"] + " {a,b}",
                        self.invocation["command"] + " ~", self.invocation["command"] + " # comment"):
            with self.subTest(command=command):
                self.assert_denied(self.call_event(self.execution_event(self.invocation | {"command": command})))

    def test_exact_prompt_approval_and_revocation_use_real_store(self):
        response = self.call_event(self.prompt_event(f" \n approve-files sha256:{self.digest}\t "))
        self.assertIsInstance(response, dict)
        self.assertIn("hookSpecificOutput", response)
        self.assertEqual(response["hookSpecificOutput"], {"hookEventName": "UserPromptSubmit",
                         "additionalContext": "Files approved; execution permission is unchanged."})
        self.assertEqual(approved_candidate(self.state, self.invocation), self.prepared["manifest"])
        response = self.call_event(self.prompt_event(f"revoke-files sha256:{self.digest}"))
        self.assertEqual(response["hookSpecificOutput"]["additionalContext"],
                         "Files revoked; execution permission is unchanged.")
        self.assertIs(load_json(next((self.state / "receipts").iterdir()).read_bytes())["active"], False)

    def test_precommit_failed_approval_does_not_change_receipts(self):
        for event in (self.prompt_event() | {"session_id": ""}, self.prompt_event() | {"session_id": 1},
                      self.prompt_event() | {"test_human_delivery": False},
                      self.prompt_event("approve-files sha256:" + "0" * 64)):
            before = self.receipts()
            with self.subTest(event=event):
                self.assert_denied(self.call_event(event), "UserPromptSubmit")
                self.assertEqual(self.receipts(), before)
        self.script.write_text("drifted\n")
        self.assert_denied(self.call_event(self.prompt_event()), "UserPromptSubmit")
        self.assertEqual(self.receipts(), {})

    def test_post_commit_fsync_failure_blocks_but_receipt_is_active(self):
        """Characterize acknowledgment ambiguity after a real atomic replacement."""
        receipt_directory_inode = (self.state / "receipts").stat().st_ino
        original_fsync = os.fsync

        def fail_receipt_directory_sync(fd):
            if os.fstat(fd).st_ino == receipt_directory_inode:
                raise OSError("synthetic failure after receipt replacement")
            original_fsync(fd)

        self.assertEqual(self.receipts(), {})
        with patch("store.os.fsync", side_effect=fail_receipt_directory_sync):
            response = self.call_event(self.prompt_event())
        self.assert_denied(response, "UserPromptSubmit")
        self.assertEqual(response["reason"], "Approval request blocked; verify receipt state before continuing. Execution permission is unchanged.")
        receipt = load_json((self.state / "receipts" / f"{self.digest}.json").read_bytes())
        self.assertIs(receipt["active"], True)
        self.assertEqual(approved_candidate(self.state, self.invocation), self.prepared["manifest"])

    def test_post_commit_handler_deadline_blocks_but_receipt_is_active(self):
        """A successful real store commit can precede the handler's final check."""
        clock = 0.0

        def approve_then_expire(*args, **kwargs):
            nonlocal clock
            approve(*args, **kwargs)
            clock = 4.0

        self.assertEqual(self.receipts(), {})
        with patch.object(hook, "approve", side_effect=approve_then_expire), \
                patch("hook.time.monotonic", side_effect=lambda: clock):
            response = self.call_event(self.prompt_event(), deadline=3.0)
        self.assert_denied(response, "UserPromptSubmit")
        self.assertEqual(response["reason"], "Approval request blocked; verify receipt state before continuing. Execution permission is unchanged.")
        receipt = load_json((self.state / "receipts" / f"{self.digest}.json").read_bytes())
        self.assertIs(receipt["active"], True)
        self.assertEqual(approved_candidate(self.state, self.invocation), self.prepared["manifest"])

    def test_ordinary_and_near_miss_prompts_never_touch_receipts(self):
        prompts = ["approved", "approve-files failed; explain why", "revoke-files usage please",
                   "approve-files sha256:short", f"approve-files sha256:{self.digest} extra",
                   "Please " + f"approve-files sha256:{self.digest}",
                   f"approve-files sha256:{self.digest.upper()}"]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.call_event(self.prompt_event(prompt), synthetic=False), {})
                self.assertEqual(self.receipts(), {})

    def test_lock_contention_denies_within_shared_internal_budget(self):
        approve(self.state, self.digest, "synthetic-direct-store-setup")
        with (self.state / "store.lock").open("rb") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            response = self.call_event(self.execution_event(), deadline=started + 0.05)
            elapsed = time.monotonic() - started
        self.assert_denied(response)
        self.assertLess(elapsed, 0.5)

    def helper_invocation(self, command=None):
        return self.invocation | {"command": command or f"/usr/bin/python3.11 {self.release / 'prepare.py'} --request {self.request}"}

    def test_only_exact_intact_release_preparation_is_exempt(self):
        with patch.object(hook, "RELEASE_METADATA_PATH", self.metadata):
            self.assertIs(hook.is_preparation_invocation(self.helper_invocation(), deadline=time.monotonic() + 3), True)
            self.assertIs(hook.is_preparation_invocation(self.invocation, deadline=time.monotonic() + 3), False)
        before = self.receipts()
        self.assertEqual(self.call_event(self.execution_event(self.helper_invocation())), {})
        self.assertEqual(self.receipts(), before)

    def test_helper_wrong_interpreter_extra_arguments_or_nonabsolute_request_deny(self):
        commands = [f"/usr/bin/bash {self.release / 'prepare.py'} --request {self.request}",
                    self.helper_invocation()["command"] + " extra",
                    f"/usr/bin/python3.11 {self.release / 'prepare.py'} --request relative.json",
                    f"/usr/bin/python3.11 {self.release / 'prepare.py'} --run {self.request}"]
        for command in commands:
            with self.subTest(command=command), patch.object(hook, "RELEASE_METADATA_PATH", self.metadata):
                with self.assertRaises(IntegrityError):
                    hook.is_preparation_invocation(self.helper_invocation(command), deadline=time.monotonic() + 3)

    def test_changed_release_module_or_bad_metadata_denies_exemption(self):
        for name in self.release_info["checksums"]:
            path = self.release / name
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.subTest(module=name):
                self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))
            path.write_bytes(original)
        variants = [self.release_info | {"unexpected": True}, self.release_info | {"hook_path": str(self.base / "hook.py")},
                    self.release_info | {"prepare_path": str(self.base / "prepare.py")},
                    self.release_info | {"checksums": {"prepare.py": "0" * 64}},
                    self.release_info | {"checksums": self.release_info["checksums"] | {"hook.py": "not-a-hash"}},
                    self.release_info | {"checksums": self.release_info["checksums"] | {"hook.py": None}}]
        for metadata in variants:
            self.metadata.write_bytes(canonical_bytes(metadata))
            with self.subTest(metadata=metadata):
                self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))

    def test_symlink_release_and_helper_aliases_cannot_gain_exemption(self):
        alias = self.base / "release-alias"
        alias.symlink_to(self.release, target_is_directory=True)
        with patch.object(hook, "RELEASE_METADATA_PATH", alias / "release-metadata.json"):
            with self.assertRaises(IntegrityError):
                hook.is_preparation_invocation(self.helper_invocation(), deadline=time.monotonic() + 3)
        helper_alias = self.base / "prepare.py"
        helper_alias.symlink_to(self.release / "prepare.py")
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation(
            f"/usr/bin/python3.11 {helper_alias} --request {self.request}"))))
        interpreter_alias = self.base / "python-alias"
        interpreter_alias.symlink_to("/usr/bin/python3.11")
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation(
            f"{interpreter_alias} {self.release / 'prepare.py'} --request {self.request}"))))

    def test_missing_or_symlinked_metadata_cannot_gain_exemption(self):
        stored_metadata = self.release / "metadata.saved"
        self.metadata.rename(stored_metadata)
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))
        self.metadata.symlink_to(stored_metadata)
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))

    def test_symlinked_module_or_recorded_interpreter_cannot_gain_exemption(self):
        module = self.release / "configure.py"
        original = module.read_bytes()
        stored_module = self.base / "configure.saved"
        module.rename(stored_module)
        module.symlink_to(stored_module)
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))
        module.unlink()
        module.write_bytes(original)
        interpreter = self.base / "python-alias"
        interpreter.symlink_to("/usr/bin/python3.11")
        self.metadata.write_bytes(canonical_bytes(self.release_info | {"python_path": str(interpreter)}))
        self.assert_denied(self.call_event(self.execution_event(self.helper_invocation(
            f"{interpreter} {self.release / 'prepare.py'} --request {self.request}"))))

    def test_unrelated_regular_prepare_script_requires_ordinary_approval(self):
        script = self.base / "prepare.py"
        script.write_bytes(self.script.read_bytes())
        invocation = self.invocation | {"command": f"/usr/bin/python3.11 {script} --request {self.request}"}
        event = self.execution_event(invocation)
        self.assert_denied(self.call_event(event))
        self.request.write_bytes(canonical_bytes({"invocation": invocation, "roots": [str(script)]}))
        prepared = prepare_request(self.request, self.state)
        approve(self.state, prepared["digest"], "synthetic-direct-store-setup")
        self.assertEqual(self.call_event(event), {})

    def test_module_modified_after_hashing_denies_exemption(self):
        target = self.release / "hook.py"
        final_inode = (self.release / "store.py").stat().st_ino
        original_read = os.read
        changed = False

        def change_earlier_module(fd, count):
            nonlocal changed
            block = original_read(fd, count)
            if not block and os.fstat(fd).st_ino == final_inode and not changed:
                target.write_bytes(target.read_bytes() + b"changed after hashing")
                changed = True
            return block

        with patch("hook.os.read", side_effect=change_earlier_module):
            self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))
        self.assertTrue(changed, "the fixture must actually change the previously hashed module")

    def test_release_parent_rebound_after_hashing_denies_exemption(self):
        final_inode = (self.release / "store.py").stat().st_ino
        original_read = os.read
        moved_release = self.base / "moved-release"

        def move_release(fd, count):
            block = original_read(fd, count)
            if not block and os.fstat(fd).st_ino == final_inode and not moved_release.exists():
                self.release.rename(moved_release)
                self.release.mkdir()
            return block

        with patch("hook.os.read", side_effect=move_release):
            self.assert_denied(self.call_event(self.execution_event(self.helper_invocation())))
        self.assertTrue(moved_release.exists(), "the fixture must actually replace the release parent")

    def protocol_fixture(self, event_name):
        if event_name == "PreToolUse":
            approve(self.state, self.digest, "synthetic-direct-store-setup")
            event, success = self.execution_event(), {}
        else:
            event = self.prompt_event()
            success = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                       "additionalContext": "Files approved; execution permission is unchanged."}}
        return self.state, self.metadata, event, success

    def run_main_bytes(self, event_name, raw, state, metadata, failure=None, *, synthetic=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(hook, "STATE_DIR", state))
            stack.enter_context(patch.object(hook, "RELEASE_METADATA_PATH", metadata))
            stack.enter_context(patch("sys.stdin", io.TextIOWrapper(io.BytesIO(raw))))
            stack.enter_context(patch("sys.stdout", stdout))
            stack.enter_context(patch("sys.stderr", stderr))
            if synthetic:
                stack.enter_context(patch.object(hook, "normalize_invocation", synthetic_normalize))
                stack.enter_context(patch.object(hook, "require_supported_prompt_delivery", synthetic_prompt_delivery))
            if failure == "exception":
                stack.enter_context(patch.object(hook, "handle_event", side_effect=RuntimeError("private detail")))
            elif failure == "deadline":
                clock = iter([0.0])
                stack.enter_context(patch("hook.time.monotonic", side_effect=lambda: next(clock, 4.0)))
            code = hook.main(["--event", event_name])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        response = json.loads(stdout.getvalue())
        self.assertIsInstance(response, dict)
        self.assertNotIn("private detail", stdout.getvalue())
        self.assertNotIn('"updatedInput"', stdout.getvalue())
        return response

    def test_main_protocol_matrix(self):
        for name in ("PreToolUse", "UserPromptSubmit"):
            state, metadata, event, success = self.protocol_fixture(name)
            valid = json.dumps(event).encode()
            for label, raw, failure in (("malformed", b"{", None), ("oversize", b" " * (1024 * 1024 + 1), None),
                                        ("exception", valid, "exception"), ("deadline", valid, "deadline")):
                before = self.receipts()
                with self.subTest(event=name, case=label):
                    self.assert_denied(self.run_main_bytes(name, raw, state, metadata, failure), name)
                    self.assertEqual(self.receipts(), before)
            self.assertEqual(self.run_main_bytes(name, valid, state, metadata), success)

    def test_main_mismatched_event_and_duplicate_keys_use_registration_schema(self):
        for name, other in (("PreToolUse", "UserPromptSubmit"), ("UserPromptSubmit", "PreToolUse")):
            before = self.receipts()
            for raw in (json.dumps({"hook_event_name": other}).encode(), b'{"x":1,"x":2}', b'[]'):
                with self.subTest(event=name, raw=raw):
                    self.assert_denied(self.run_main_bytes(name, raw, self.state, self.metadata), name)
            self.assertEqual(self.receipts(), before)

    def test_main_production_refusal_and_ordinary_prompt(self):
        for name, event in (("PreToolUse", self.execution_event()), ("UserPromptSubmit", self.prompt_event())):
            self.assert_denied(self.run_main_bytes(name, json.dumps(event).encode(), self.state, self.metadata, synthetic=False), name)
        self.assertEqual(self.run_main_bytes("UserPromptSubmit", json.dumps(self.prompt_event("hello")).encode(),
                                            self.state, self.metadata, synthetic=False), {})
        self.assertEqual(self.receipts(), {})

    def test_main_reads_at_most_one_mib_plus_overflow_byte(self):
        raw = io.BytesIO(b" " * (1024 * 1024 + 500))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.TextIOWrapper(raw)), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(hook.main(["--event", "PreToolUse"]), 0)
            self.assertEqual(raw.tell(), 1024 * 1024 + 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assert_denied(json.loads(stdout.getvalue()))

    def test_main_budget_includes_time_consumed_reading_stdin(self):
        clock = 0.0

        class DelayedInput(io.BytesIO):
            def read(self, count=-1):
                nonlocal clock
                result = super().read(count)
                clock = 4.0
                return result

        raw = DelayedInput(json.dumps(self.prompt_event("ordinary prompt")).encode())
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", io.TextIOWrapper(raw)), patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
                patch("hook.time.monotonic", side_effect=lambda: clock):
            self.assertEqual(hook.main(["--event", "UserPromptSubmit"]), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assert_denied(json.loads(stdout.getvalue()), "UserPromptSubmit")
        self.assertEqual(self.receipts(), {})

    def test_main_startup_errors_do_not_enable_production_overrides(self):
        variants = [[], ["--event", "unknown"], ["--event", "PreToolUse", "--synthetic"],
                    ["--event", "PreToolUse", "--state-dir", str(self.state)],
                    ["--event", "PreToolUse", "--release-metadata", str(self.metadata)]]
        for args in variants:
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(args=args), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as result:
                    hook.main(args)
                self.assertEqual(result.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")

    def test_real_subprocess_protocol_failures_and_test_only_successes(self):
        bootstrap = "\n".join([
            "import sys", "from pathlib import Path", "import hook",
            "sys.path.insert(0, str(Path.cwd() / 'tests'))",
            "from test_hook import synthetic_normalize, synthetic_prompt_delivery",
            "hook.STATE_DIR = Path(sys.argv[1])", "hook.RELEASE_METADATA_PATH = Path(sys.argv[2])",
            "hook.normalize_invocation = synthetic_normalize",
            "hook.require_supported_prompt_delivery = synthetic_prompt_delivery",
            "raise SystemExit(hook.main(['--event', sys.argv[3]]))",
        ])
        for name in ("PreToolUse", "UserPromptSubmit"):
            state, metadata, event, success = self.protocol_fixture(name)
            for raw in (b"{", b" " * (1024 * 1024 + 1)):
                with self.subTest(event=name, case="registered-failure", size=len(raw)):
                    result = subprocess.run([sys.executable, "hook.py", "--event", name], input=raw, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stderr, b"")
                    self.assertEqual(len(result.stdout.splitlines()), 1)
                    self.assert_denied(json.loads(result.stdout), name)
            result = subprocess.run([sys.executable, "-c", bootstrap, str(state), str(metadata), name],
                                    input=json.dumps(event).encode(), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(json.loads(result.stdout), success)


if __name__ == "__main__":
    unittest.main()
