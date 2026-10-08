"""Synthetic registration fixtures and temporary offline installation only."""

import copy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import configure
from configure import merge_hooks, registrations, remove_hooks
import hook
from manifest import canonical_bytes, load_json
from policy import IntegrityError
from prepare import prepare_request
from store import approved_candidate
from test_hook import synthetic_normalize, synthetic_prompt_delivery

FILES = ("policy.py", "manifest.py", "store.py", "hook.py", "prepare.py", "configure.py")


def synthetic_release(root=Path("/opt/example-hook/v1")):
    return {"python_path": "/usr/bin/python3.11", "hook_path": str(root / "hook.py"),
            "prepare_path": str(root / "prepare.py"), "checksums": {name: "0" * 64 for name in FILES}}


class RegistrationTests(unittest.TestCase):
    def test_merge_and_remove_preserve_other_guard(self):
        existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}]}}
        release = {"python_path": "/usr/bin/python3.11",
                   "hook_path": "/opt/example-hook/v1/hook.py",
                   "prepare_path": "/opt/example-hook/v1/prepare.py",
                   "checksums": {name: "0" * 64 for name in
                                 ("policy.py", "manifest.py", "store.py",
                                  "hook.py", "prepare.py", "configure.py")}}
        ours = registrations(release)
        merged = merge_hooks(existing, ours)
        self.assertEqual(merge_hooks(merged, ours), merged)
        self.assertEqual(remove_hooks(merged, ours), existing)

    def test_registration_quotes_identities_and_sets_both_synchronous_event_modes(self):
        release = synthetic_release(Path("/opt/synthetic release/v1"))
        release["python_path"] = "/opt/synthetic python/python3.11"
        ours = registrations(release)
        self.assertEqual(set(ours.get("hooks", {})), {"PreToolUse", "UserPromptSubmit"})
        for event in ("PreToolUse", "UserPromptSubmit"):
            group = ours["hooks"][event][0]
            self.assertEqual(set(group), {"matcher", "hooks"} if event == "PreToolUse" else {"hooks"})
            if event == "PreToolUse":
                self.assertEqual(group["matcher"], "Bash")
            command = group["hooks"][0]
            self.assertEqual(command["type"], "command")
            self.assertIs(command["async"], False)
            self.assertEqual(command["timeout"], 5)
            self.assertEqual(shlex.split(command["command"]),
                             ["/opt/synthetic python/python3.11", "/opt/synthetic release/v1/hook.py", "--event", event])

    def test_mixed_groups_other_events_and_unknown_keys_survive_without_input_mutation(self):
        ours = registrations(synthetic_release())
        self.assertIn("hooks", ours)
        guard = {"type": "command", "command": "/usr/bin/true", "timeout": 7}
        existing = {"note": {"retain": True}, "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [guard, *ours["hooks"]["PreToolUse"][0]["hooks"]]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/false"}], "note": "retain"}]}}
        before = copy.deepcopy(existing)
        merged = merge_hooks(existing, ours)
        self.assertEqual(existing, before)
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 1)
        removed = remove_hooks(merged, ours)
        self.assertEqual(merged, merge_hooks(merged, ours))
        self.assertEqual(removed, {"note": {"retain": True}, "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [guard]}], "Stop": before["hooks"]["Stop"]}})

    def test_malformed_configuration_and_release_metadata_are_refused(self):
        for existing in ({"hooks": []}, {"hooks": {"PreToolUse": {}}},
                         {"hooks": {"PreToolUse": [None]}}, {"hooks": {"PreToolUse": [{"hooks": "command"}]}}):
            for operation in (merge_hooks, remove_hooks):
                with self.subTest(existing=existing, operation=operation), self.assertRaises(IntegrityError):
                    operation(existing, registrations(synthetic_release()))
        for definition in ({}, {"type": "command"}, {"type": "command", "command": None},
                           {"type": "command", "command": "/usr/bin/true", "async": 0},
                           {"type": "command", "command": "/usr/bin/true", "timeout": "5"}):
            existing = {"hooks": {"Stop": [{"hooks": [definition]}]}}
            for operation in (merge_hooks, remove_hooks):
                with self.subTest(definition=definition, operation=operation), self.assertRaises(IntegrityError):
                    operation(existing, registrations(synthetic_release()))
        good = synthetic_release()
        for release in (good | {"version": 1}, good | {"python_path": "relative"},
                        good | {"prepare_path": "/other/prepare.py"},
                        good | {"checksums": {"hook.py": "0" * 64}},
                        good | {"checksums": good["checksums"] | {"hook.py": "A" * 64}}):
            with self.subTest(release=release), self.assertRaises(IntegrityError):
                registrations(release)

    def test_owned_definition_types_and_preserved_empty_metadata_are_exact(self):
        ours = registrations(synthetic_release())
        for field, value in (("async", 0), ("timeout", 5.0)):
            wrong = copy.deepcopy(ours)
            wrong["hooks"]["PreToolUse"][0]["hooks"][0][field] = value
            for operation in (merge_hooks, remove_hooks):
                with self.subTest(field=field, operation=operation), self.assertRaises(IntegrityError):
                    operation(wrong, ours)
        existing = {"hooks": {"PreToolUse": [{"matcher": "Bash", "note": "keep", "hooks": []}]}}
        self.assertEqual(remove_hooks(merge_hooks(existing, ours), ours), existing)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "synthetic source"
        self.source.mkdir()
        for name in FILES:
            (self.source / name).write_bytes(f"# synthetic offline module: {name}\n".encode())
        (self.source / "not-runtime.txt").write_text("must not be installed")
        self.release = self.base / "synthetic releases" / "v1"
        self.config = self.base / "hooks.json"
        self.original = {"other_setting": "retain", "hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}}
        self.config.write_bytes(canonical_bytes(self.original))
        self.config.chmod(0o640)
        self.marker = self.base / "protected-command-was-run"
        self.addCleanup(lambda: self.assertFalse(self.marker.exists()))

    def install(self, source=None):
        result = configure.install(self.config, self.release, source or self.source)
        self.assertIn("release", result, "offline installation must return the recorded identities")
        return result

    def metadata(self):
        return load_json((self.release / "release-metadata.json").read_bytes())

    def invocation(self, command):
        return {"tool": "Bash", "command": command, "workdir": str(self.base),
                "shell": "/usr/bin/bash", "login": False, "tty": False}

    def test_preview_proposes_diff_and_writes_nothing(self):
        before = {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        real_open = os.open

        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR), "preview must not open for writing")
            return real_open(path, flags, *args, **kwargs)

        with patch("configure.os.open", side_effect=readonly):
            diff = configure.preview(self.config, self.release, self.source)
        self.assertIn("--event PreToolUse", diff)
        self.assertIn("--event UserPromptSubmit", diff)
        self.assertIn(str(self.release / "hook.py"), diff)
        self.assertEqual({p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}, before)
        self.assertFalse(self.release.parent.exists())

    def test_install_and_remove_are_idempotent_preserve_guard_permissions_and_state(self):
        state = self.base / "state" / "receipts"
        state.mkdir(parents=True)
        receipt = state / "synthetic-receipt.json"
        receipt.write_bytes(b"unchanged approval-state sentinel")
        result = self.install()
        metadata = result["release"]
        self.assertEqual(set(metadata), {"python_path", "hook_path", "prepare_path", "checksums"})
        self.assertEqual(metadata["python_path"], str(Path(sys.executable).resolve()))
        self.assertEqual(metadata["hook_path"], str(self.release / "hook.py"))
        self.assertEqual(metadata["prepare_path"], str(self.release / "prepare.py"))
        self.assertEqual(set(metadata["checksums"]), set(FILES))
        self.assertEqual(set(p.name for p in self.release.iterdir()), {*FILES, "release-metadata.json"})
        self.assertEqual(stat.S_IMODE(self.release.stat().st_mode), 0o700)
        for name in FILES:
            self.assertEqual((self.release / name).read_bytes(), (self.source / name).read_bytes())
            self.assertEqual(metadata["checksums"][name], hashlib.sha256((self.source / name).read_bytes()).hexdigest())
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), canonical_bytes(self.original))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        before = self.config.stat()
        module_before = (self.release / "hook.py").stat()
        again = self.install()
        self.assertIsNone(again["backup"])
        self.assertEqual(self.config.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((self.release / "hook.py").stat().st_ino, module_before.st_ino)
        configure.remove(self.config, self.release)
        self.assertEqual(load_json(self.config.read_bytes()), self.original)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        removed_before = self.config.stat()
        configure.remove(self.config, self.release)
        self.assertEqual(self.config.stat().st_mtime_ns, removed_before.st_mtime_ns)
        self.assertEqual(receipt.read_bytes(), b"unchanged approval-state sentinel")
        self.assertTrue(self.release.exists())

    def test_release_checksum_or_recorded_registration_disagreement_is_not_repaired(self):
        self.install()
        before = self.config.read_bytes()
        target = self.release / "configure.py"
        target.write_bytes(b"changed release bytes")
        with self.assertRaisesRegex(IntegrityError, "new.*version|different|checksum"):
            configure.install(self.config, self.release, self.source)
        self.assertEqual(target.read_bytes(), b"changed release bytes")
        self.assertEqual(self.config.read_bytes(), before)
        target.write_bytes((self.source / "configure.py").read_bytes())
        existing = load_json(before)
        own = existing["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        own["timeout"] = 9
        self.config.write_bytes(canonical_bytes(existing))
        modified = self.config.read_bytes()
        for operation in (lambda: configure.install(self.config, self.release, self.source),
                          lambda: configure.remove(self.config, self.release)):
            with self.assertRaisesRegex(IntegrityError, "disagree|owned|definition"):
                operation()
            self.assertEqual(self.config.read_bytes(), modified)

    def test_concurrent_config_change_aborts_without_overwriting_the_editor(self):
        changed = canonical_bytes(self.original | {"editor_change": "retain"})
        real_fsync = os.fsync
        triggered = []

        def edit_after_private_sync(fd):
            result = real_fsync(fd)
            path = os.readlink(f"/proc/self/fd/{fd}")
            if ".hooks-update-" in path and not triggered:
                triggered.append(True)
                self.config.write_bytes(changed)
            return result

        with patch("configure.os.fsync", side_effect=edit_after_private_sync), \
                self.assertRaisesRegex(IntegrityError, "concurrent|changed"):
            configure.install(self.config, self.release, self.source)
        self.assertTrue(triggered)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_remove_retains_later_edits_and_can_rollback_damaged_runtime(self):
        self.install()
        edited = load_json(self.config.read_bytes())
        edited["later_setting"] = {"retain": True}
        guard = {"type": "command", "command": "/usr/bin/false", "timeout": 8}
        edited["hooks"]["PreToolUse"][0]["hooks"].append(guard)
        self.config.write_bytes(canonical_bytes(edited))
        (self.release / "hook.py").write_bytes(b"damaged runtime does not prevent exact rollback")
        with patch("configure._python_identity", side_effect=AssertionError("remove must use recorded identity")):
            configure.remove(self.config, self.release)
        expected = copy.deepcopy(self.original)
        expected["later_setting"] = {"retain": True}
        expected["hooks"]["PreToolUse"][0]["hooks"].append(guard)
        self.assertEqual(load_json(self.config.read_bytes()), expected)

    def test_remove_concurrent_editor_change_is_retained(self):
        self.install()
        changed = canonical_bytes(load_json(self.config.read_bytes()) | {"later_editor": True})
        real_fsync = os.fsync
        triggered = []

        def edit_after_sync(fd):
            result = real_fsync(fd)
            if ".hooks-update-" in os.readlink(f"/proc/self/fd/{fd}") and not triggered:
                triggered.append(True)
                self.config.write_bytes(changed)
            return result

        with patch("configure.os.fsync", side_effect=edit_after_sync), \
                self.assertRaisesRegex(IntegrityError, "concurrent|changed"):
            configure.remove(self.config, self.release)
        self.assertTrue(triggered)
        self.assertEqual(self.config.read_bytes(), changed)

    def test_interrupted_copy_is_unregistered_and_never_repaired_in_place(self):
        real_write = configure._Parent.write_new

        def fail_copy(parent, name, raw, **kwargs):
            if name == "manifest.py":
                raise OSError("synthetic interrupted release copy")
            return real_write(parent, name, raw, **kwargs)

        before = self.config.read_bytes()
        with patch.object(configure._Parent, "write_new", new=fail_copy), self.assertRaises(IntegrityError):
            configure.install(self.config, self.release, self.source)
        self.assertEqual(self.config.read_bytes(), before)
        partial = {p.name: p.read_bytes() for p in self.release.iterdir()}
        self.assertEqual(set(partial), {"policy.py"})
        with self.assertRaisesRegex(IntegrityError, "incomplete|new.*version"):
            configure.install(self.config, self.release, self.source)
        self.assertEqual({p.name: p.read_bytes() for p in self.release.iterdir()}, partial)
        self.assertEqual(self.config.read_bytes(), before)

    def test_release_directory_replacement_before_copy_is_refused(self):
        real_fsync = os.fsync
        moved = self.base / "moved-created-release"
        triggered = []

        def replace_after_mkdir(fd):
            result = real_fsync(fd)
            if os.readlink(f"/proc/self/fd/{fd}") == str(self.release.parent) and not triggered:
                triggered.append(True)
                self.release.rename(moved)
                self.release.mkdir(mode=0o700)
                (self.release / "editor-sentinel").write_bytes(b"retain replacement directory")
            return result

        before = self.config.read_bytes()
        with patch("configure.os.fsync", side_effect=replace_after_mkdir), \
                self.assertRaises(IntegrityError):
            configure.install(self.config, self.release, self.source)
        self.assertTrue(triggered)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(set(p.name for p in self.release.iterdir()), {"editor-sentinel"})
        self.assertEqual((self.release / "editor-sentinel").read_bytes(), b"retain replacement directory")
        self.assertEqual(list(moved.iterdir()), [])

    def test_existing_release_metadata_disagreement_is_not_repaired(self):
        self.install()
        metadata = self.metadata()
        metadata["python_path"] = "/usr/bin/synthetic-other-python"
        path = self.release / "release-metadata.json"
        path.write_bytes(canonical_bytes(metadata))
        config_before = self.config.read_bytes()
        metadata_before = path.read_bytes()
        with self.assertRaisesRegex(IntegrityError, "different|disagree"):
            configure.install(self.config, self.release, self.source)
        with self.assertRaisesRegex(IntegrityError, "owned|disagree"):
            configure.remove(self.config, self.release)
        self.assertEqual(path.read_bytes(), metadata_before)
        self.assertEqual(self.config.read_bytes(), config_before)

    def test_deadline_expires_before_writes_without_resetting_nested_operations(self):
        real_read = configure._read
        now = [100.0]
        reads = []

        def expensive_read(path, deadline):
            reads.append(deadline)
            result = real_read(path, deadline)
            now[0] += 0.8
            return result

        before = self.config.read_bytes()
        with patch("configure.time.monotonic", side_effect=lambda: now[0]), \
                patch("configure._read", side_effect=expensive_read), \
                self.assertRaisesRegex(IntegrityError, "deadline"):
            configure.install(self.config, self.release, self.source)
        self.assertGreater(len(reads), 1)
        self.assertEqual(set(reads), {100.0 + configure.CHECK_SECONDS})
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.release.parent.exists())

    def test_missing_malformed_duplicate_keys_and_inline_coexistence_are_actionable(self):
        before = self.config.read_bytes()
        for raw in (b"{", b'{"hooks":{},"hooks":{}}'):
            self.config.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(IntegrityError, "JSON|configuration|duplicate"):
                configure.preview(self.config, self.release, self.source)
            self.assertEqual(self.config.read_bytes(), raw)
        self.config.write_bytes(before)
        missing = self.source / "store.py"
        saved = missing.read_bytes()
        missing.unlink()
        with self.assertRaisesRegex(IntegrityError, "missing|runtime|read"):
            configure.preview(self.config, self.release, self.source)
        missing.write_bytes(saved)
        with self.assertRaisesRegex(IntegrityError, "missing|configuration|read"):
            configure.preview(self.base / "absent-hooks.json", self.release, self.source)
        inline = self.base / "config.toml"
        inline.write_text('[hooks]\nPreToolUse = []\n')
        with self.assertRaisesRegex(IntegrityError, "inline.*config.toml|coexist"):
            configure.install(self.config, self.release, self.source)
        self.assertEqual(inline.read_text(), '[hooks]\nPreToolUse = []\n')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.release.exists())
        inline.write_text('[hooks.state.synthetic]\ntrusted = true\n')
        inline_before = inline.read_bytes()
        self.install()
        self.assertEqual(inline.read_bytes(), inline_before)

    def test_symlinked_config_and_runtime_paths_are_refused(self):
        original = self.config.with_name("original.json")
        self.config.rename(original)
        self.config.symlink_to(original)
        with self.assertRaises(IntegrityError):
            configure.install(self.config, self.release, self.source)
        self.assertEqual(original.read_bytes(), canonical_bytes(self.original))
        self.config.unlink()
        original.rename(self.config)
        module = self.source / "store.py"
        original_module = module.with_name("other.py")
        module.rename(original_module)
        module.symlink_to(original_module)
        with self.assertRaises(IntegrityError):
            configure.install(self.config, self.release, self.source)
        self.assertFalse(self.release.exists())

    def test_registered_event_commands_drive_real_hook_main_and_preserve_permission(self):
        self.install(Path(configure.__file__).parent)
        installed = load_json(self.config.read_bytes())
        for event, want in (("PreToolUse", "deny"), ("UserPromptSubmit", None)):
            groups = installed["hooks"][event]
            owned = next(h for g in groups for h in g["hooks"] if "--event" in h.get("command", ""))
            argv = shlex.split(owned["command"])
            self.assertEqual(argv, [self.metadata()["python_path"], str(self.release / "hook.py"), "--event", event])
            data = {"hook_event_name": event, "prompt": "synthetic ordinary question"}
            result = subprocess.run(argv, input=canonical_bytes(data), capture_output=True, timeout=5,
                                    env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, b"")
            response = load_json(result.stdout)
            if want:
                self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], want)
            else:
                self.assertEqual(response, {})

    def test_installed_release_and_removal_preserve_real_approval_and_unrelated_hooks(self):
        self.install(Path(configure.__file__).parent)
        state = self.base / "state"
        script = self.base / "run.py"
        script.write_text(f"from pathlib import Path\nPath({str(self.marker)!r}).touch()\n")
        invocation = self.invocation(shlex.join(["/usr/bin/python3.11", str(script)]))
        request = self.base / "request.json"
        request.write_bytes(canonical_bytes({"invocation": invocation, "roots": [str(script)]}))
        prepared = prepare_request(request, state)
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "synthetic-installed-release",
                  "test_human_delivery": True, "prompt": f"approve-files sha256:{prepared['digest']}"}
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/not-effective-workdir",
                 "tool_input": {key: value for key, value in invocation.items() if key != "tool"}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(hook, "RELEASE_METADATA_PATH", self.release / "release-metadata.json"))
            stack.enter_context(patch.object(hook, "normalize_invocation", synthetic_normalize))
            stack.enter_context(patch.object(hook, "require_supported_prompt_delivery", synthetic_prompt_delivery))
            self.assertEqual(hook.handle_event(event, state)["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(hook.handle_event(prompt, state)["hookSpecificOutput"]["additionalContext"],
                             "Files approved; execution permission is unchanged.")
            self.assertEqual(hook.handle_event(event, state), {})
            helper = self.metadata()
            helper_event = copy.deepcopy(event)
            helper_event["tool_input"]["command"] = shlex.join(
                [helper["python_path"], helper["prepare_path"], "--request", str(request)])
            self.assertEqual(hook.handle_event(helper_event, state), {})

        receipt = state / "receipts" / f"{prepared['digest']}.json"
        receipt_before = receipt.read_bytes()
        release_before = {name: (self.release / name).read_bytes() for name in (*FILES, "release-metadata.json")}
        installed = load_json(self.config.read_bytes())
        unrelated = {"hooks": [{"type": "command", "command": "/usr/bin/true", "timeout": 8}]}
        installed["hooks"]["UserPromptSubmit"].append(unrelated)
        self.config.write_bytes(canonical_bytes(installed))
        configure.remove(self.config, self.release)
        expected = copy.deepcopy(self.original)
        expected["hooks"]["UserPromptSubmit"] = [unrelated]
        self.assertEqual(load_json(self.config.read_bytes()), expected)
        self.assertEqual(receipt.read_bytes(), receipt_before)
        self.assertEqual(approved_candidate(state, invocation), prepared["manifest"])
        self.assertEqual({name: (self.release / name).read_bytes() for name in release_before}, release_before)

    def test_exact_release_metadata_identity_is_shared_with_housekeeping_checker(self):
        self.install()
        metadata = self.metadata()
        request = self.base / "request.json"
        request.write_bytes(b"{}")
        helper = metadata["prepare_path"]
        python = metadata["python_path"]
        command = shlex.join([python, helper, "--request", str(request)])
        deadline = time.monotonic() + 3
        with patch.object(hook, "RELEASE_METADATA_PATH", self.release / "release-metadata.json"):
            self.assertTrue(hook.is_preparation_invocation(self.invocation(command), deadline=deadline))
            for argv in (["/usr/bin/bash", helper, "--request", str(request)],
                         [python, "-I", helper, "--request", str(request)],
                         [python, helper, "--request", str(request), "extra"]):
                with self.subTest(argv=argv):
                    try:
                        answer = hook.is_preparation_invocation(self.invocation(shlex.join(argv)), deadline=deadline)
                    except IntegrityError:
                        answer = False
                    self.assertIs(answer, False)
            other = self.base / "other" / "prepare.py"
            other.parent.mkdir()
            other.write_bytes(b"# distinct synthetic helper\n")
            alias = self.base / "helper-alias.py"
            alias.symlink_to(helper)
            python_alias = self.base / "python3.11"
            python_alias.symlink_to(python)
            for argv in ([python, str(other), "--request", str(request)],
                         [python, str(alias), "--request", str(request)],
                         [str(python_alias), helper, "--request", str(request)]):
                with self.subTest(argv=argv):
                    try:
                        answer = hook.is_preparation_invocation(self.invocation(shlex.join(argv)), deadline=deadline)
                    except IntegrityError:
                        answer = False
                    self.assertIs(answer, False)
            (self.release / "manifest.py").write_bytes(b"changed checksum\n")
            with self.assertRaises(IntegrityError):
                hook.is_preparation_invocation(self.invocation(command), deadline=deadline)

    def test_production_cli_install_is_blocked_without_a_capability_override(self):
        before = self.config.read_bytes()
        output = io.StringIO()
        with patch.object(configure, "CONFIG_PATH", self.config), \
                patch.object(configure, "RELEASE_DIR", self.release), \
                patch.object(configure, "SOURCE_DIR", self.source), redirect_stderr(output):
            self.assertEqual(configure.main(["install"]), 1)
            self.assertIn("unsupported", output.getvalue().lower())
            self.assertFalse(self.release.exists())
            self.assertEqual(self.config.read_bytes(), before)
            for args in (["install", "--force"], ["install", "--host-supported"], ["--config", str(self.config), "install"]):
                with self.assertRaises(SystemExit) as exc:
                    configure.main(args)
                self.assertEqual(exc.exception.code, 2)

    def test_production_cli_startup_does_not_write_local_bytecode(self):
        source = self.base / "real-cli-source"
        source.mkdir()
        for name in FILES:
            (source / name).write_bytes((Path(configure.__file__).parent / name).read_bytes())
        # Synthetic CLI copy with temporary constants, never production overrides.
        cli = source / "configure.py"
        text = cli.read_text()
        text = text.replace('CONFIG_PATH = Path("/home/user/.codex/hooks.json")',
                            f"CONFIG_PATH = Path({str(self.config)!r})")
        text = text.replace('RELEASE_DIR = Path("/home/user/.local/share/execution-integrity-hook/v1")',
                            f"RELEASE_DIR = Path({str(self.release)!r})")
        cli.write_text(text)
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
        for action in ("preview", "install"):
            with self.subTest(action=action):
                result = subprocess.run([sys.executable, str(cli), action],
                                        capture_output=True, env=environment, timeout=5)
                self.assertEqual(result.returncode, 0 if action == "preview" else 1)
                if action == "install":
                    self.assertIn(b"unsupported", result.stderr.lower())
                    self.assertEqual(result.stdout, b"")
                else:
                    self.assertIn(b"--event PreToolUse", result.stdout)
                    self.assertEqual(result.stderr, b"")
        self.assertFalse((source / "__pycache__").exists())
        self.assertEqual({p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}, before)
        self.assertFalse(self.release.parent.exists())

    def test_incidental_runtime_cache_does_not_change_recorded_release_identity(self):
        self.install()
        cache = self.release / "__pycache__"
        cache.mkdir()
        sentinel = cache / "synthetic-unused-cache.pyc"
        sentinel.write_bytes(b"incidental unused test cache")
        config_before = self.config.read_bytes()
        result = self.install()
        self.assertIsNone(result["backup"])
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(sentinel.read_bytes(), b"incidental unused test cache")
