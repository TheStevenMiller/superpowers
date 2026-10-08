"""Offline, synthetic approvals; no host or protected-command execution."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from manifest import canonical_bytes, invocation_key, load_json, manifest_digest, prepare_manifest
from policy import IntegrityError
import prepare
from prepare import prepare_request
import store
from store import approve, approved_candidate, publish_candidate, revoke


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.script = self.base / "run.py"
        self.script.write_text("print(1)\n")
        self.state = self.base / "state"
        self.inv = {"tool": "Bash", "command": f"/usr/bin/python3.11 {self.script}",
                    "workdir": str(self.base), "shell": "/usr/bin/bash",
                    "login": False, "tty": False}

    def candidate(self, invocation=None, roots=None):
        return prepare_manifest({"invocation": invocation or self.inv,
                                 "roots": roots or [str(self.script)]})

    def publish_and_approve(self, candidate=None):
        digest = publish_candidate(self.state, candidate or self.candidate())
        approve(self.state, digest, "synthetic-user-session")
        return digest

    def receipt(self, digest):
        return load_json((self.state / "receipts" / f"{digest}.json").read_bytes())

    def test_replacing_candidate_cannot_reuse_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            src = base / "run.py"
            src.write_text("print(1)\n")
            inv = {"tool": "Bash", "command": f"/usr/bin/python3.11 {src}",
                   "workdir": d, "shell": "/usr/bin/bash", "login": False, "tty": False}
            candidate = prepare_manifest({"invocation": inv, "roots": [str(src)]})
            state = base / "state"
            digest = publish_candidate(state, candidate)
            approve(state, digest, "synthetic-user-session")
            src.write_text("print(2)\n")
            replacement = prepare_manifest({"invocation": inv, "roots": [str(src)]})
            (state / "candidates" / f"{digest}.json").write_bytes(canonical_bytes(replacement))
            with self.assertRaises(IntegrityError):
                approved_candidate(state, inv)

    def test_publication_creates_no_receipt_and_is_private(self):
        candidate = self.candidate()
        digest = publish_candidate(self.state, candidate)
        self.assertEqual(list((self.state / "receipts").iterdir()), [])
        self.assertEqual((self.state / "candidates" / f"{digest}.json").read_bytes(),
                         canonical_bytes(candidate))
        for path in (self.state, self.state / "candidates", self.state / "receipts"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "store.lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / "candidates" / f"{digest}.json").stat().st_mode), 0o600)
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, self.inv)

    def test_approval_receipt_schema_and_binding(self):
        candidate = self.candidate()
        digest = self.publish_and_approve(candidate)
        receipt = self.receipt(digest)
        self.assertEqual(set(receipt), {"version", "digest", "invocation_key", "active",
                                        "source_session", "updated_at"})
        self.assertEqual(receipt["version"], 1)
        self.assertEqual(receipt["digest"], digest)
        self.assertEqual(receipt["invocation_key"], invocation_key(self.inv))
        self.assertIs(receipt["active"], True)
        self.assertEqual(receipt["source_session"], "synthetic-user-session")
        self.assertIsInstance(receipt["updated_at"], str)
        self.assertEqual(approved_candidate(self.state, self.inv), candidate)
        self.assertEqual(stat.S_IMODE((self.state / "receipts" / f"{digest}.json").stat().st_mode), 0o600)

    def test_unapproved_new_content_does_not_reuse_previous_digest(self):
        old = self.publish_and_approve()
        self.script.write_text("print(2)\n")
        new = publish_candidate(self.state, self.candidate())
        self.assertNotEqual(new, old)
        self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), old)
        self.assertFalse((self.state / "receipts" / f"{new}.json").exists())

    def test_approval_rejects_drift_without_receipt(self):
        digest = publish_candidate(self.state, self.candidate())
        self.script.write_text("print(2)\n")
        with self.assertRaises(IntegrityError):
            approve(self.state, digest, "synthetic-user-session")
        self.assertEqual(list((self.state / "receipts").iterdir()), [])

    def test_revocation_blocks_and_explicit_reapproval_reactivates(self):
        digest = self.publish_and_approve()
        revoke(self.state, digest, "synthetic-revocation")
        self.assertIs(self.receipt(digest)["active"], False)
        self.assertEqual(self.receipt(digest)["source_session"], "synthetic-revocation")
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, self.inv)
        approve(self.state, digest, "synthetic-reapproval")
        self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), digest)

    def test_new_approval_supersedes_only_same_invocation(self):
        old = self.publish_and_approve()
        other_inv = self.inv | {"command": self.inv["command"] + " literal-argument"}
        other = self.publish_and_approve(self.candidate(other_inv))
        self.script.write_text("print(2)\n")
        new = self.publish_and_approve()
        self.assertIs(self.receipt(old)["active"], False)
        self.assertIs(self.receipt(new)["active"], True)
        self.assertIs(self.receipt(other)["active"], True)
        self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), new)

    def test_identical_command_receipts_are_scoped_to_effective_workdir(self):
        projects = [self.base / "project-one", self.base / "project-two"]
        for project in projects:
            project.mkdir()
        first_inv, second_inv = (self.inv | {"workdir": str(project)} for project in projects)
        first_candidate, second_candidate = self.candidate(first_inv), self.candidate(second_inv)
        self.assertEqual(first_candidate["files"], second_candidate["files"])
        self.assertEqual(first_inv["command"], second_inv["command"])
        first = publish_candidate(self.state, first_candidate)
        second = publish_candidate(self.state, second_candidate)
        approve(self.state, first, "synthetic-first-project")
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, second_inv)
        approve(self.state, second, "synthetic-second-project")
        self.assertEqual(approved_candidate(self.state, first_inv), first_candidate)
        self.assertEqual(approved_candidate(self.state, second_inv), second_candidate)
        revoke(self.state, first, "synthetic-first-project-revocation")
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, first_inv)
        self.assertEqual(approved_candidate(self.state, second_inv), second_candidate)
        self.assertIs(self.receipt(second)["active"], True)

    def test_invalid_policy_publication_has_no_state_side_effect(self):
        for changes in ({"command": "/usr/bin/true"}, {"command": "/usr/bin/bash -c echo"},
                        {"command": self.inv["command"] + " ; echo unsafe"}, {"login": True},
                        {"tty": True}, {"tool": "Other"}, {"workdir": "relative"}):
            candidate = self.candidate()
            candidate["invocation"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                publish_candidate(self.state, candidate)
        candidate = self.candidate()
        candidate["invocation"]["command"] = f"/usr/bin/python3.11 {self.base / 'missing.py'}"
        with self.assertRaises(IntegrityError):
            publish_candidate(self.state, candidate)
        self.assertFalse(self.state.exists())

    def test_injected_invalid_candidates_do_not_deactivate_valid_approval(self):
        valid = self.publish_and_approve()
        before = (self.state / "receipts" / f"{valid}.json").read_bytes()
        for changes in ({"command": "/usr/bin/true"}, {"login": True}, {"tty": True},
                        {"command": f"/usr/bin/python3.11 {self.base / 'missing.py'}"}):
            candidate = self.candidate()
            candidate["invocation"].update(changes)
            digest = manifest_digest(candidate)
            path = self.state / "candidates" / f"{digest}.json"
            path.write_bytes(canonical_bytes(candidate))
            path.chmod(0o600)
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                approve(self.state, digest, "synthetic-user-session")
            self.assertEqual((self.state / "receipts" / f"{valid}.json").read_bytes(), before)

    def test_receipt_corruption_and_multiple_active_bindings_deny(self):
        first = self.publish_and_approve()
        path = self.state / "receipts" / f"{first}.json"
        original = path.read_bytes()
        for receipt in (self.receipt(first) | {"digest": "0" * 64},
                        self.receipt(first) | {"invocation_key": "0" * 64},
                        self.receipt(first) | {"active": 1},
                        self.receipt(first) | {"extra": True}):
            path.write_bytes(canonical_bytes(receipt))
            with self.subTest(receipt=receipt), self.assertRaises(IntegrityError):
                approved_candidate(self.state, self.inv)
            path.write_bytes(original)
        self.script.write_text("print(2)\n")
        second = self.publish_and_approve()
        receipt = self.receipt(first)
        receipt["active"] = True
        path.write_bytes(canonical_bytes(receipt))
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, self.inv)
        self.assertIs(self.receipt(second)["active"], True)

    def test_invalid_digest_and_source_do_not_touch_store(self):
        for digest in ("../escape", "A" * 64, "g" * 64, "a" * 63, 1):
            for operation in (approve, revoke):
                with self.subTest(digest=digest, operation=operation), self.assertRaises(IntegrityError):
                    operation(self.state, digest, "synthetic-user-session")
        with self.assertRaises(IntegrityError):
            approve(self.state, "a" * 64, "")
        self.assertFalse(self.state.exists())

    def test_expired_supplied_deadline_never_resets(self):
        candidate = self.candidate()
        digest = self.publish_and_approve(candidate)
        for operation in (lambda: publish_candidate(self.state, candidate, deadline=0),
                          lambda: approve(self.state, digest, "synthetic-user-session", deadline=0),
                          lambda: revoke(self.state, digest, "synthetic-user-session", deadline=0),
                          lambda: approved_candidate(self.state, self.inv, deadline=0)):
            with self.subTest(operation=operation), self.assertRaises(IntegrityError):
                operation()

    def test_lock_contention_denies_within_supplied_budget(self):
        digest = self.publish_and_approve()
        with (self.state / "store.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(IntegrityError):
                approve(self.state, digest, "synthetic-user-session", deadline=started + 0.08)
            self.assertLess(time.monotonic() - started, 0.5)

    def test_symlinked_state_and_authoritative_files_are_refused(self):
        real = self.base / "real"
        real.mkdir()
        self.state.symlink_to(real, target_is_directory=True)
        with self.assertRaises(IntegrityError):
            publish_candidate(self.state, self.candidate())
        self.assertEqual(list(real.iterdir()), [])
        self.state.unlink()
        digest = self.publish_and_approve()
        for path in (self.state / "candidates" / f"{digest}.json",
                     self.state / "receipts" / f"{digest}.json", self.state / "store.lock"):
            saved = path.with_name(path.name + ".saved")
            path.rename(saved)
            path.symlink_to(saved)
            with self.subTest(path=path), self.assertRaises(IntegrityError):
                approved_candidate(self.state, self.inv)
            path.unlink()
            saved.rename(path)

    def test_identical_publication_is_idempotent_without_resnapshot(self):
        candidate = self.candidate()
        digest = publish_candidate(self.state, candidate)
        path = self.state / "candidates" / f"{digest}.json"
        previous = path.stat()
        self.script.unlink()
        self.assertEqual(publish_candidate(self.state, candidate), digest)
        self.assertEqual(path.stat().st_ino, previous.st_ino)
        self.assertEqual(path.stat().st_mtime_ns, previous.st_mtime_ns)

    def test_existing_corrupt_candidate_is_never_repaired(self):
        candidate = self.candidate()
        digest = publish_candidate(self.state, candidate)
        path = self.state / "candidates" / f"{digest}.json"
        for raw in (b"{", canonical_bytes(candidate | {"extra": True}),
                    canonical_bytes(candidate | {"invocation": self.inv | {"command": self.inv["command"] + " arg"}})):
            path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(IntegrityError):
                publish_candidate(self.state, candidate)
            self.assertEqual(path.read_bytes(), raw)

    def test_interrupted_private_write_leaves_no_authoritative_candidate(self):
        candidate = self.candidate()
        triggered = []
        real_write = os.write

        def fail_write(fd, raw):
            triggered.append(True)
            real_write(fd, raw[:1])
            raise OSError("synthetic interrupted private write")

        with patch("store.os.write", side_effect=fail_write), self.assertRaises(IntegrityError):
            publish_candidate(self.state, candidate)
        self.assertTrue(triggered)
        self.assertEqual(list((self.state / "candidates").glob("*.json")), [])

    def test_interruptions_before_and_after_link_leave_only_complete_authority(self):
        candidate = self.candidate()
        digest = manifest_digest(candidate)
        real_link = os.link
        for after in (False, True):
            state = self.base / ("after-link" if after else "before-link")
            triggered = []

            def interrupted(*args, **kwargs):
                triggered.append(True)
                if after:
                    real_link(*args, **kwargs)
                raise OSError("synthetic interrupted link")

            with patch("store.os.link", side_effect=interrupted), self.assertRaises(IntegrityError):
                publish_candidate(state, candidate)
            self.assertTrue(triggered)
            path = state / "candidates" / f"{digest}.json"
            self.assertEqual(path.exists(), after)
            if after:
                self.assertEqual(path.read_bytes(), canonical_bytes(candidate))
            self.assertEqual(publish_candidate(state, candidate), digest)

    def test_concurrent_identical_publishers_leave_one_complete_candidate(self):
        candidate = self.candidate()
        with ThreadPoolExecutor(max_workers=4) as executor:
            digests = list(executor.map(lambda _: publish_candidate(self.state, candidate), range(8)))
        self.assertEqual(len(set(digests)), 1)
        names = list((self.state / "candidates").iterdir())
        self.assertEqual([p.name for p in names], [f"{digests[0]}.json"])
        self.assertEqual(names[0].read_bytes(), canonical_bytes(candidate))

    def test_approval_holds_lock_during_live_verification_and_serializes_readers(self):
        self.publish_and_approve()
        self.script.write_text("print(2)\n")
        digest = publish_candidate(self.state, self.candidate())
        entered, release = threading.Event(), threading.Event()
        real_verify = store.verify_manifest if hasattr(store, "verify_manifest") else lambda *a, **k: None

        def paused(candidate, *, deadline):
            entered.set()
            if not release.wait(1):
                raise AssertionError("test synchronization timeout")
            return real_verify(candidate, deadline=deadline)

        with patch.object(store, "verify_manifest", side_effect=paused, create=True):
            with ThreadPoolExecutor(max_workers=2) as executor:
                future = executor.submit(approve, self.state, digest, "synthetic-user-session")
                try:
                    self.assertTrue(entered.wait(0.2), "approval must freshly verify under the lock")
                    with self.assertRaises(IntegrityError):
                        approved_candidate(self.state, self.inv, deadline=time.monotonic() + 0.05)
                finally:
                    release.set()
                future.result()
        self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), digest)

    def test_crashed_supersession_with_zero_active_receipts_denies(self):
        old = self.publish_and_approve()
        self.script.write_text("print(2)\n")
        new = publish_candidate(self.state, self.candidate())
        real_replace = os.replace
        triggered = []

        def interrupted(src, dst, *args, **kwargs):
            if dst == f"{new}.json":
                triggered.append(True)
                raise OSError("synthetic crashed receipt replacement")
            return real_replace(src, dst, *args, **kwargs)

        with patch("store.os.replace", side_effect=interrupted), self.assertRaises(IntegrityError):
            approve(self.state, new, "synthetic-user-session")
        self.assertTrue(triggered)
        self.assertIs(self.receipt(old)["active"], False)
        with self.assertRaises(IntegrityError):
            approved_candidate(self.state, self.inv)

    def test_concurrent_approvals_expose_only_a_unique_final_receipt(self):
        config = self.base / "config.json"
        config.write_text("{}\n")
        first = publish_candidate(self.state, self.candidate())
        second = publish_candidate(self.state, self.candidate(roots=[str(self.script), str(config)]))
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        real_verify = store.verify_manifest
        seen = []

        def paused(candidate, *, deadline):
            seen.append(manifest_digest(candidate))
            if len(seen) == 1:
                entered.set()
                if not release.wait(1):
                    raise AssertionError("test synchronization timeout")
            return real_verify(candidate, deadline=deadline)

        def competing_approval():
            second_started.set()
            approve(self.state, second, "synthetic-second-approval")

        with patch.object(store, "verify_manifest", side_effect=paused):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(approve, self.state, first, "synthetic-first-approval")
                try:
                    self.assertTrue(entered.wait(0.5))
                    second_future = executor.submit(competing_approval)
                    self.assertTrue(second_started.wait(0.5))
                    with self.assertRaises(IntegrityError):
                        approved_candidate(self.state, self.inv, deadline=time.monotonic() + 0.05)
                    self.assertEqual(list((self.state / "receipts").glob("*.json")), [])
                finally:
                    release.set()
                first_future.result()
                second_future.result()
        self.assertEqual(seen, [first, second])
        self.assertIs(self.receipt(first)["active"], False)
        self.assertIs(self.receipt(second)["active"], True)
        self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), second)

    def test_regular_orphans_are_ignored_but_symlinked_temporary_paths_deny(self):
        digest = self.publish_and_approve()
        for folder in ("candidates", "receipts"):
            orphan = self.state / folder / ".tmp-synthetic-orphan"
            orphan.write_bytes(b"partial non-authoritative bytes")
            orphan.chmod(0o600)
            self.assertEqual(manifest_digest(approved_candidate(self.state, self.inv)), digest)
            orphan.unlink()
            orphan.symlink_to(self.script)
            with self.subTest(folder=folder), self.assertRaises(IntegrityError):
                approved_candidate(self.state, self.inv)
            orphan.unlink()

    def test_nonprivate_authoritative_state_is_refused_without_chmod_repair(self):
        candidate = self.candidate()
        digest = self.publish_and_approve(candidate)
        for path in (self.state, self.state / "candidates", self.state / "receipts",
                     self.state / "store.lock", self.state / "candidates" / f"{digest}.json",
                     self.state / "receipts" / f"{digest}.json"):
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | 0o044)
            with self.subTest(path=path), self.assertRaises(IntegrityError):
                approved_candidate(self.state, self.inv)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode | 0o044)
            path.chmod(mode)

    def request_file(self, invocation=None, roots=None):
        path = self.base / "request.json"
        path.write_bytes(canonical_bytes({"invocation": invocation or self.inv,
                                         "roots": roots or [str(self.script)]}))
        return path

    def test_prepare_request_returns_digest_manifest_and_never_receipt(self):
        path = self.request_file()
        result = prepare_request(path, self.state)
        self.assertEqual(set(result), {"digest", "manifest"})
        self.assertEqual(result["digest"], manifest_digest(result["manifest"]))
        self.assertEqual(result["manifest"]["invocation"], self.inv)
        self.assertEqual(list((self.state / "receipts").iterdir()), [])
        self.assertEqual(prepare_request(path, self.state)["digest"], result["digest"])

    def test_prepare_policy_rejections_create_no_candidate(self):
        for changes in ({"command": "/usr/bin/env /usr/bin/python3.11 /script.py"},
                        {"command": "/usr/bin/python3.11 -c pass"}, {"login": True},
                        {"tty": True}, {"command": self.inv["command"] + " | cat"},
                        {"command": f"/usr/bin/python3.11 {self.base / 'not-snapshotted.py'}"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                prepare_request(self.request_file(self.inv | changes), self.state)
        self.assertFalse(self.state.exists())

    def test_prepare_parse_and_nested_work_share_one_deadline(self):
        path = self.request_file()
        with self.assertRaises(IntegrityError):
            prepare_request(path, self.state, deadline=0)
        real_load = prepare.load_json if hasattr(prepare, "load_json") else load_json
        now = [0.0]

        def consume_budget(raw):
            value = real_load(raw)
            now[0] = 2.0
            return value

        with patch("prepare.time.monotonic", side_effect=lambda: now[0], create=True), \
                patch("manifest.time.monotonic", side_effect=lambda: now[0]), \
                patch.object(prepare, "load_json", side_effect=consume_budget, create=True):
            with self.assertRaises(IntegrityError):
                prepare_request(path, self.state, deadline=1.0)

    def test_prepare_cli_has_fixed_state_and_no_execution_or_approval_options(self):
        sentinel = self.base / "executed"
        self.script.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n")
        path = self.request_file()
        output = io.StringIO()
        with patch.object(prepare, "STATE_DIR", self.state), redirect_stdout(output):
            self.assertEqual(prepare.main(["--request", str(path)]), 0)
        self.assertTrue(output.getvalue(), "helper must print the review summary")
        review = load_json(output.getvalue().encode())
        self.assertEqual(set(review), {"digest", "invocation", "roots", "files"})
        self.assertEqual(review["invocation"], self.inv)
        self.assertEqual(review["roots"], [str(self.script)])
        self.assertEqual([item["path"] for item in review["files"]], [str(self.script)])
        self.assertFalse(sentinel.exists())
        digest = review["digest"]
        approve(self.state, digest, "synthetic-user-session")
        approved_candidate(self.state, self.inv)
        revoke(self.state, digest, "synthetic-user-session")
        self.assertFalse(sentinel.exists(), "offline store/helper operations must never run the script")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            for extra in (["--state-dir", str(self.state)], ["--output", str(self.base / "out")],
                          ["--approve"], ["--run"], ["--exec"], ["approve"], ["run"]):
                with self.subTest(extra=extra), self.assertRaises(SystemExit) as exc:
                    prepare.main(["--request", str(path), *extra])
                self.assertEqual(exc.exception.code, 2)
