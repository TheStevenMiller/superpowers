"""Synchronous hook protocol; real host context/provenance remains unsupported."""

import argparse
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time

from manifest import CHECK_SECONDS, MAX_BYTES, canonical_bytes, load_json, verify_manifest
from policy import (
    Event, IntegrityError, Invocation, Response, literal_argv,
    validate_absolute_path, validate_command,
)
from store import approve, approved_candidate, revoke

STATE_DIR = Path("/home/user/.local/state/execution-integrity-hook")
RELEASE_METADATA_PATH = Path(__file__).absolute().with_name("release-metadata.json")
MAX_EVENT_BYTES = 1024 * 1024
_RELEASE_MODULES = {"policy.py", "manifest.py", "store.py", "prepare.py", "hook.py", "configure.py"}
_EXECUTION_FAILURE = "Integrity check blocked; verify supported host context and approved, unchanged files."
_PROMPT_FAILURE = "Approval request blocked; verify receipt state before continuing. Execution permission is unchanged."


def _check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise IntegrityError("hook check deadline exceeded")


def normalize_invocation(event: Event) -> Invocation:
    """No reviewed adapter currently establishes effective native context."""
    raise IntegrityError("effective host execution context is unsupported")


def require_supported_prompt_delivery(event: Event) -> None:
    """Session IDs and event-supplied flags cannot authenticate a human."""
    raise IntegrityError("authenticated human prompt delivery is unsupported")


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _file_metadata(metadata):
    return _identity(metadata), metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


class _ReleaseReader:
    """Keep no-follow path bindings alive through the whole release check.

    Installed releases are deliberately forbidden as ordinary snapshot roots,
    so this narrow reader checks only the fixed release evidence and interpreter.
    Blocking I/O remains outside the cooperative deadline guarantee.
    """

    def __init__(self, stack: ExitStack, deadline: float):
        self.stack = stack
        self.deadline = deadline
        self.bindings = []
        self.total_bytes = 0
        _check(deadline)
        self.anchor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        stack.callback(os.close, self.anchor)

    def open_regular(self, path: str) -> int:
        validate_absolute_path(path)
        parts = path[1:].split("/")
        parent = self.anchor
        for index, name in enumerate(parts):
            _check(self.deadline)
            directory = index < len(parts) - 1
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not (stat.S_ISDIR(before.st_mode) if directory else stat.S_ISREG(before.st_mode)):
                raise IntegrityError("release path is symlinked or not regular")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            flags |= os.O_DIRECTORY if directory else os.O_NONBLOCK
            fd = os.open(name, flags, dir_fd=parent)
            self.stack.callback(os.close, fd)
            after = os.fstat(fd)
            if _file_metadata(before) != _file_metadata(after):
                raise IntegrityError("release path changed before open")
            self.bindings.append((parent, name, fd, after, directory))
            parent = fd
        _check(self.deadline)
        return parent

    def blocks(self, fd: int, *, limit: int):
        size = 0
        while True:
            _check(self.deadline)
            block = os.read(fd, min(65536, limit - size + 1))
            _check(self.deadline)
            if not block:
                break
            size += len(block)
            self.total_bytes += len(block)
            if size > limit or self.total_bytes > MAX_BYTES:
                raise IntegrityError("release evidence exceeds byte limit")
            yield block

    def recheck(self) -> None:
        for parent, name, fd, before, directory in reversed(self.bindings):
            _check(self.deadline)
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            descriptor = os.fstat(fd)
            signature = _identity if directory else _file_metadata
            if signature(current) != signature(before) or signature(descriptor) != signature(before):
                raise IntegrityError("release evidence changed during check")
        _check(self.deadline)


def _release_metadata(reader: _ReleaseReader) -> dict:
    path = str(RELEASE_METADATA_PATH)
    fd = reader.open_regular(path)
    metadata = load_json(b"".join(reader.blocks(fd, limit=MAX_EVENT_BYTES)))
    if set(metadata) != {"python_path", "hook_path", "prepare_path", "checksums"}:
        raise IntegrityError("invalid release metadata fields")
    for key in ("python_path", "hook_path", "prepare_path"):
        validate_absolute_path(metadata[key])
    root = RELEASE_METADATA_PATH.parent
    if metadata["hook_path"] != str(root / "hook.py") or metadata["prepare_path"] != str(root / "prepare.py"):
        raise IntegrityError("release metadata paths do not identify adjacent helpers")
    checksums = metadata["checksums"]
    if not isinstance(checksums, dict) or set(checksums) != _RELEASE_MODULES:
        raise IntegrityError("invalid release checksum fields")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in checksums.values()):
        raise IntegrityError("invalid release checksum value")
    return metadata


def is_preparation_invocation(invocation: Invocation, *, deadline: float) -> bool:
    """Recognize one intact housekeeping command, never execute or approve it."""
    _check(deadline)
    argv = literal_argv(invocation)
    try:
        with ExitStack() as stack:
            reader = _ReleaseReader(stack, deadline)
            metadata = _release_metadata(reader)
            if len(argv) < 2 or argv[1] != metadata["prepare_path"]:
                reader.recheck()
                return False
            if len(argv) != 4 or argv[0] != metadata["python_path"] or argv[2] != "--request":
                raise IntegrityError("installed preparation command does not match")
            validate_absolute_path(argv[3])
            reader.open_regular(metadata["python_path"])
            for name, expected in sorted(metadata["checksums"].items()):
                fd = reader.open_regular(str(RELEASE_METADATA_PATH.parent / name))
                digest = hashlib.sha256()
                for block in reader.blocks(fd, limit=MAX_BYTES):
                    digest.update(block)
                if digest.hexdigest() != expected:
                    raise IntegrityError("installed release checksum changed")
            reader.recheck()
            return True
    except OSError as exc:
        raise IntegrityError("unable to read release evidence safely") from exc


def deny(reason: str) -> Response:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _failure(event_name: str) -> Response:
    if event_name == "UserPromptSubmit":
        return {"decision": "block", "reason": _PROMPT_FAILURE}
    return deny(_EXECUTION_FAILURE)


def check_execution(event: Event, state_dir: Path, *, deadline: float) -> Response:
    invocation = normalize_invocation(event)
    if is_preparation_invocation(invocation, deadline=deadline):
        return {}
    validate_command(invocation)
    candidate = approved_candidate(state_dir, invocation, deadline=deadline)
    verify_manifest(candidate, deadline=deadline)
    return {}


def _handle_prompt(event: Event, state_dir: Path, *, deadline: float) -> Response:
    prompt = event.get("prompt")
    if not isinstance(prompt, str):
        raise IntegrityError("prompt is missing or invalid")
    match = re.fullmatch(r"(approve|revoke)-files sha256:([0-9a-f]{64})", prompt.strip())
    if match is None:
        return {}
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise IntegrityError("session provenance is missing")
    require_supported_prompt_delivery(event)
    operation = approve if match[1] == "approve" else revoke
    operation(state_dir, match[2], session_id, deadline=deadline)
    action = "approved" if match[1] == "approve" else "revoked"
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
            "additionalContext": f"Files {action}; execution permission is unchanged."}}


def handle_event(event: Event, state_dir: Path, *, deadline: float | None = None) -> Response:
    deadline = time.monotonic() + CHECK_SECONDS if deadline is None else deadline
    event_name = event.get("hook_event_name") if isinstance(event, dict) else None
    try:
        _check(deadline)
        if event_name == "PreToolUse":
            response = check_execution(event, state_dir, deadline=deadline)
        elif event_name == "UserPromptSubmit":
            response = _handle_prompt(event, state_dir, deadline=deadline)
        else:
            raise IntegrityError("unsupported event")
        _check(deadline)
        return response
    except Exception:
        return _failure(event_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check file integrity without granting execution permission.")
    parser.add_argument("--event", choices=("PreToolUse", "UserPromptSubmit"), required=True)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + CHECK_SECONDS
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        _check(deadline)
        if len(raw) > MAX_EVENT_BYTES:
            raise IntegrityError("event exceeds byte limit")
        event = load_json(raw)
        if event.get("hook_event_name") != args.event:
            raise IntegrityError("input event does not match registration")
        response = handle_event(event, STATE_DIR, deadline=deadline)
    except Exception:
        response = _failure(args.event)
    print(canonical_bytes(response).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
