"""Content drift, malformed state and deterministic filesystem-race checks."""

import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from manifest import (
    canonical_bytes, invocation_key, load_json, manifest_digest,
    prepare_manifest, snapshot, validate_manifest_schema, verify_manifest,
)
from policy import IntegrityError

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class JsonTests(unittest.TestCase):
    def test_canonical_utf8_bytes_and_digest_are_content_bound(self):
        self.assertEqual(canonical_bytes({"z": "é", "a": 1}), b'{"a":1,"z":"\xc3\xa9"}')
        self.assertEqual(manifest_digest({}), "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")

    def test_json_duplicate_keys_are_rejected_at_every_depth(self):
        for raw in (b'{"a":1,"a":2}', b'{"nested":{"a":1,"a":2}}'):
            with self.subTest(raw=raw), self.assertRaises(IntegrityError):
                load_json(raw)

    def test_json_invalid_numbers_encoding_and_top_level_are_rejected(self):
        for raw in (b'[]', b'1', b'null', b'{', b'{"x":"\xff"}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(IntegrityError):
                load_json(raw)
        self.assertEqual(load_json(b'{"a":{"b":2}}'), {"a": {"b": 2}})

    def test_non_json_canonical_values_are_rejected(self):
        for value in ({"x": float("nan")}, {"x": float("inf")}, {"x": object()}):
            with self.subTest(value=value), self.assertRaises(IntegrityError):
                canonical_bytes(value)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.script = self.base / "run.py"
        self.script.write_bytes(b"abc")

    def request(self, roots=None):
        return {
            "invocation": {"tool": "Bash", "command": f"/usr/bin/python3.11 {self.script}",
                           "workdir": str(self.base), "shell": "/usr/bin/bash",
                           "login": False, "tty": False},
            "roots": roots if roots is not None else [str(self.script)],
        }

    def candidate(self, roots=None):
        result = prepare_manifest(self.request(roots))
        self.assertIsInstance(result, dict, "preparation must return a manifest")
        return result

    def take_snapshot(self, roots=None):
        return snapshot(roots or [str(self.script)], deadline=time.monotonic() + 3.0)

    def test_prepare_contains_exact_sorted_content_and_does_not_mutate_request(self):
        config = self.base / "config.json"
        config.write_bytes(b"abc")
        request = self.request([str(self.script), str(config)])
        original = copy.deepcopy(request)
        result = prepare_manifest(request)
        self.assertEqual(result, {
            "version": 1, "invocation": original["invocation"],
            "roots": [str(config), str(self.script)],
            "files": [{"path": str(config), "sha256": ABC_SHA256},
                      {"path": str(self.script), "sha256": ABC_SHA256}],
        })
        self.assertEqual(request, original)
        verify_manifest(result)

    def test_changed_bytes_with_restored_mtime_are_rejected(self):
        candidate = prepare_manifest(self.request())
        previous = self.script.stat()
        self.script.write_bytes(b"xyz")
        os.utime(self.script, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)

    def test_config_changes_are_rejected(self):
        config = self.base / "config.json"
        config.write_bytes(b"abc")
        candidate = self.candidate([str(self.script), str(config)])
        config.write_bytes(b"xyz")
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)

    def test_undeclared_project_siblings_are_not_part_of_the_attestation(self):
        sibling = self.base / "undeclared-config.json"
        sibling.write_bytes(b"first undeclared content")
        candidate = self.candidate()
        self.assertEqual(candidate["roots"], [str(self.script)])
        self.assertEqual(candidate["files"], [{"path": str(self.script), "sha256": ABC_SHA256}])
        original = copy.deepcopy(candidate)
        sibling.write_bytes(b"different undeclared content")
        verify_manifest(candidate)
        self.assertEqual(candidate, original)
        self.script.write_bytes(b"changed declared content")
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)
        self.assertEqual(candidate, original)

    def test_schema_validation_checks_policy_without_reading_declared_files(self):
        candidate = self.candidate()
        self.script.unlink()
        validate_manifest_schema(candidate)
        candidate["invocation"]["command"] = "/usr/bin/true"
        with self.assertRaises(IntegrityError):
            validate_manifest_schema(candidate)

    def test_directory_additions_and_deletions_are_rejected(self):
        candidate = self.candidate([str(self.base)])
        extra = self.base / ".hidden-input"
        extra.write_bytes(b"abc")
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)
        extra.unlink()
        self.script.unlink()
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)

    def test_empty_directory_existence_between_checks_is_not_attested(self):
        existing_empty = self.base / "pre-existing-empty"
        existing_empty.mkdir()
        candidate = self.candidate([str(self.base)])
        self.assertEqual(candidate["files"], [{"path": str(self.script), "sha256": ABC_SHA256}])
        original = copy.deepcopy(candidate)

        existing_empty.rmdir()
        verify_manifest(candidate)
        self.assertEqual(self.candidate([str(self.base)]), original)

        added_empty = self.base / "new-empty"
        added_empty.mkdir()
        verify_manifest(candidate)
        self.assertEqual(self.candidate([str(self.base)]), original)

        (added_empty / "input.json").write_bytes(b"abc")
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)
        self.assertEqual(candidate, original)

    def test_recursive_snapshot_includes_hidden_files(self):
        child = self.base / "child"
        child.mkdir()
        hidden = child / ".input"
        hidden.write_bytes(b"abc")
        self.assertEqual(self.take_snapshot([str(self.base)]), [
            {"path": str(hidden), "sha256": ABC_SHA256},
            {"path": str(self.script), "sha256": ABC_SHA256},
        ])

    def test_invocation_key_binds_actual_context_and_exact_command_text(self):
        invocation = self.request()["invocation"]
        original = invocation_key(invocation)
        self.assertIsInstance(original, str)
        for change in ({"workdir": "/other/worktree"}, {"command": invocation["command"] + " "},
                       {"shell": "/other/bash"}, {"login": True}, {"tty": True}, {"tool": "Other"}):
            with self.subTest(change=change):
                self.assertNotEqual(invocation_key(invocation | change), original)

    def test_same_bytes_in_different_worktrees_do_not_share_manifest(self):
        first = self.candidate()
        other = self.base / "other-worktree"
        other.mkdir()
        script = other / "run.py"
        script.write_bytes(b"abc")
        request = self.request([str(script)])
        request["invocation"] |= {"workdir": str(other), "command": f"/usr/bin/python3.11 {script}"}
        second = prepare_manifest(request)
        self.assertNotEqual(manifest_digest(first), manifest_digest(second))

    def test_prepare_rejects_unknown_fields_and_wrong_types(self):
        variants = [[], {}, self.request() | {"extra": True}, self.request() | {"roots": "bad"},
                    self.request() | {"roots": [1]}]
        for field, value in (("tool", 1), ("command", None), ("workdir", []), ("shell", 1), ("login", 0), ("tty", 0)):
            request = self.request()
            request["invocation"][field] = value
            variants.append(request)
        request = self.request()
        request["invocation"]["extra"] = "bad"
        variants.append(request)
        request = self.request()
        del request["invocation"]["shell"]
        variants.append(request)
        for request in variants:
            with self.subTest(request=request), self.assertRaises(IntegrityError):
                prepare_manifest(request)

    def test_unapprovable_policy_or_missing_entrypoint_fails_prepare_and_verify(self):
        other = self.base / "other"
        other.write_bytes(b"abc")
        for request in (self.request([str(other)]), self.request() | {"invocation": self.request()["invocation"] | {"command": "/usr/bin/true"}}):
            with self.subTest(request=request), self.assertRaises(IntegrityError):
                prepare_manifest(request)
        candidate = self.candidate()
        candidate["invocation"]["command"] = "/usr/bin/true"
        with self.assertRaises(IntegrityError):
            verify_manifest(candidate)

    def test_manifest_rejects_invalid_digest_schema_and_order(self):
        good = self.candidate()
        invalids = [good | {"extra": True}, good | {"version": True}, good | {"version": 2},
                    good | {"files": []}, good | {"roots": []}]
        for digest in ("a" * 63, "A" * 64, "g" * 64, 1):
            invalids.append(good | {"files": [{"path": str(self.script), "sha256": digest}]})
        for entry in ({"path": str(self.script), "sha256": ABC_SHA256, "extra": 1},
                      {"path": str(self.base / "other"), "sha256": ABC_SHA256},
                      {"path": str(self.base) + "//run.py", "sha256": ABC_SHA256}):
            invalids.append(good | {"files": [entry]})
        invalids.append(good | {"files": good["files"] * 2})
        for candidate in invalids:
            with self.subTest(candidate=candidate), self.assertRaises(IntegrityError):
                verify_manifest(candidate)
        extra = self.base / "extra"
        extra.write_bytes(b"abc")
        multiple = self.candidate([str(extra), str(self.script)])
        for key in ("roots", "files"):
            with self.subTest(key=key), self.assertRaises(IntegrityError):
                verify_manifest(multiple | {key: list(reversed(multiple[key]))})

    def test_roots_reject_overlap_duplicates_ambiguous_and_protected_paths(self):
        invalids = [[str(self.base), str(self.script)], [str(self.script)] * 2, [],
                    ["/"], ["/home"], ["/home/user"], ["/home/user/.local"],
                    ["/home/user/.local/state/execution-integrity-hook/candidates"],
                    ["/home/user/.local/share/execution-integrity-hook/v1/hook.py"],
                    [str(self.base) + "/./run.py"], [str(self.base) + "//run.py"], [str(self.base) + "/"]]
        for roots in invalids:
            with self.subTest(roots=roots), self.assertRaises(IntegrityError):
                self.take_snapshot(roots) if roots else snapshot([], deadline=time.monotonic() + 3)

    def test_missing_unreadable_symlink_special_and_empty_inputs_deny(self):
        empty = self.base / "empty"
        empty.mkdir()
        symlink = self.base / "linked"
        symlink.symlink_to(self.script)
        directory_link = self.base / "directory-link"
        directory_link.symlink_to(empty, target_is_directory=True)
        fifo = self.base / "fifo"
        os.mkfifo(fifo)
        for root in (self.base / "missing", empty, symlink, directory_link / "anything", fifo):
            with self.subTest(root=root), self.assertRaises(IntegrityError):
                self.take_snapshot([str(root)])
        real_open = os.open

        def inaccessible(path, flags, *args, **kwargs):
            if path == "run.py":
                raise PermissionError("test-only inaccessible file")
            return real_open(path, flags, *args, **kwargs)

        with patch("manifest.os.open", side_effect=inaccessible), self.assertRaises(IntegrityError):
            self.take_snapshot()

    def test_file_count_and_total_byte_bounds_deny_instead_of_truncating(self):
        other = self.base / "other"
        other.write_bytes(b"xy")
        with patch("manifest.MAX_FILES", 2), patch("manifest.MAX_BYTES", 5):
            result = self.take_snapshot([str(self.base)])
            self.assertIsInstance(result, list)
            self.assertEqual(len(result), 2)
            other.write_bytes(b"xyz")
            with self.assertRaises(IntegrityError):
                self.take_snapshot([str(self.base)])
            other.write_bytes(b"xy")
            third = self.base / "third"
            third.write_bytes(b"")
            with self.assertRaises(IntegrityError):
                self.take_snapshot([str(self.base)])

    def test_expired_deadline_is_not_reset_by_nested_operations(self):
        with patch("manifest.time.monotonic", return_value=2):
            for operation in (lambda: snapshot([str(self.script)], deadline=1),
                              lambda: prepare_manifest(self.request(), deadline=1)):
                with self.subTest(operation=operation), self.assertRaises(IntegrityError):
                    operation()

    def test_deadline_exhaustion_during_read_denies(self):
        real_read = os.read
        now = [0]

        def exhausted_read(fd, amount):
            result = real_read(fd, amount)
            now[0] = 2
            return result

        with patch("manifest.time.monotonic", side_effect=lambda: now[0]), patch("manifest.os.read", side_effect=exhausted_read):
            with self.assertRaises(IntegrityError):
                snapshot([str(self.script)], deadline=1)

    def test_file_metadata_mutation_during_read_denies(self):
        real_read = os.read
        previous = self.script.stat()
        changed = []

        def mutate(fd, amount):
            data = real_read(fd, amount)
            if data and not changed:
                changed.append(True)
                os.utime(self.script, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
            return data

        with patch("manifest.os.read", side_effect=mutate), self.assertRaises(IntegrityError):
            self.take_snapshot()
        self.assertTrue(changed)

    def test_directory_addition_during_hashing_denies(self):
        real_read = os.read
        changed = []

        def add_member(fd, amount):
            data = real_read(fd, amount)
            if data and not changed:
                changed.append(True)
                (self.base / "added").write_bytes(b"new")
            return data

        with patch("manifest.os.read", side_effect=add_member), self.assertRaises(IntegrityError):
            self.take_snapshot([str(self.base)])
        self.assertTrue(changed)


class DescriptorRaceTests(unittest.TestCase):
    def run_race(self, component, replacement, after_read=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            parent = base / "parent"
            parent.mkdir()
            script = parent / "run.py"
            script.write_bytes(b"approved")
            substitute = base / "substitute"
            substitute.mkdir()
            substitute_file = substitute / "run.py"
            substitute_file.write_bytes(b"must never be read")
            forbidden_identities = {(substitute_file.stat().st_dev, substitute_file.stat().st_ino)}
            real_open, real_read = os.open, os.read
            triggered, forbidden_reads = [], []

            def replace():
                triggered.append(True)
                target = parent if component == "parent" else script
                target.rename(base / "original")
                if replacement == "symlink":
                    target.symlink_to(substitute if component == "parent" else substitute_file)
                elif component == "parent":
                    target.mkdir()
                    (target / "run.py").write_bytes(b"must never be read")
                    stat = (target / "run.py").stat()
                    forbidden_identities.add((stat.st_dev, stat.st_ino))
                else:
                    target.write_bytes(b"must never be read")
                    stat = target.stat()
                    forbidden_identities.add((stat.st_dev, stat.st_ino))

            def open_with_race(path, flags, *args, **kwargs):
                if not after_read and not triggered and path == component:
                    replace()
                if path != "/" and os.path.isabs(path):
                    self.fail("traversal reopened an absolute path")
                return real_open(path, flags, *args, **kwargs)

            def read_with_race(fd, amount):
                stat = os.fstat(fd)
                if (stat.st_dev, stat.st_ino) in forbidden_identities:
                    forbidden_reads.append(True)
                data = real_read(fd, amount)
                if after_read and not triggered and data:
                    replace()
                return data

            with patch("manifest.os.open", side_effect=open_with_race), patch("manifest.os.read", side_effect=read_with_race):
                with self.assertRaises(IntegrityError):
                    snapshot([str(script)], deadline=time.monotonic() + 3)
            self.assertTrue(triggered, "the intended traversal boundary was not reached")
            self.assertFalse(forbidden_reads, "substituted file contents were read")

    def test_final_file_symlink_between_stat_and_open_denies(self):
        self.run_race("run.py", "symlink")

    def test_final_file_replacement_between_stat_and_open_denies(self):
        self.run_race("run.py", "regular")

    def test_parent_symlink_between_stat_and_open_denies(self):
        self.run_race("parent", "symlink")

    def test_parent_replacement_between_stat_and_open_denies(self):
        self.run_race("parent", "regular")

    def test_parent_rename_after_descriptor_acquisition_denies(self):
        self.run_race("parent", "regular", after_read=True)

    def test_final_file_replacement_after_read_denies(self):
        self.run_race("run.py", "regular", after_read=True)


if __name__ == "__main__":
    unittest.main()
