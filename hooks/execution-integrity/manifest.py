"""Bounded, descriptor-relative content manifests; never execution approval."""

from contextlib import ExitStack
import hashlib
import json
import os
import re
import stat
import time
from typing import Any

from policy import (
    IntegrityError, Invocation, Manifest, validate_absolute_path,
    validate_command, validate_entrypoint,
)

MAX_FILES = 4096
MAX_BYTES = 134217728
CHECK_SECONDS = 3.0
PREPARE_SECONDS = 3.0
_CHUNK_BYTES = 1024 * 1024
_PROTECTED_DIRECTORIES = (
    "/home/user/.local/state/execution-integrity-hook",
    "/home/user/.local/share/execution-integrity-hook",
)
_INVOCATION_FIELDS = {"tool", "command", "workdir", "shell", "login", "tty"}


def canonical_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntegrityError("invalid JSON value") from exc


def load_json(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise IntegrityError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_number(_value):
        raise IntegrityError("non-finite JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_number)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrityError("invalid JSON input") from exc
    if not isinstance(value, dict):
        raise IntegrityError("expected JSON object")
    return value


def manifest_digest(manifest: Manifest) -> str:
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def invocation_key(invocation: Invocation) -> str:
    return hashlib.sha256(canonical_bytes(invocation)).hexdigest()


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise IntegrityError("integrity check deadline exceeded")


def _exact_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise IntegrityError(f"invalid {label} fields")


def _validate_invocation(invocation: Invocation) -> None:
    _exact_fields(invocation, _INVOCATION_FIELDS, "invocation")
    for field in ("tool", "command", "workdir", "shell"):
        if not isinstance(invocation[field], str):
            raise IntegrityError("invalid invocation value type")
    for field in ("login", "tty"):
        if type(invocation[field]) is not bool:
            raise IntegrityError("invalid invocation value type")
    validate_command(invocation)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def _validate_roots(roots: list[str], deadline: float) -> list[str]:
    if not isinstance(roots, list) or not roots:
        raise IntegrityError("expected nonempty roots list")
    seen = set()
    for path in roots:
        _check_deadline(deadline)
        validate_absolute_path(path)
        if path in {"/", "/home/user"} or any(
            _within(path, protected) or _within(protected, path)
            for protected in _PROTECTED_DIRECTORIES
        ):
            raise IntegrityError("broad or protected snapshot root")
        if path in seen:
            raise IntegrityError("duplicate snapshot root")
        seen.add(path)
    for path in seen:
        _check_deadline(deadline)
        parts = path.split("/")
        if any("/".join(parts[:index]) in seen for index in range(2, len(parts))):
            raise IntegrityError("overlapping snapshot roots")
    return sorted(roots)


def validate_manifest_schema(manifest: Manifest, *, deadline: float | None = None) -> None:
    """Validate structure and launch policy without reading declared input files."""
    deadline = time.monotonic() + CHECK_SECONDS if deadline is None else deadline
    _check_deadline(deadline)
    _exact_fields(manifest, {"version", "invocation", "roots", "files"}, "manifest")
    if type(manifest["version"]) is not int or manifest["version"] != 1:
        raise IntegrityError("unsupported manifest version")
    _validate_invocation(manifest["invocation"])
    roots = _validate_roots(manifest["roots"], deadline)
    if manifest["roots"] != roots:
        raise IntegrityError("manifest roots are not sorted")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise IntegrityError("invalid manifest file count")
    previous = None
    for entry in files:
        _check_deadline(deadline)
        _exact_fields(entry, {"path", "sha256"}, "file entry")
        path = entry["path"]
        validate_absolute_path(path)
        if previous is not None and path <= previous:
            raise IntegrityError("manifest files are unsorted or duplicated")
        if not any(_within(path, root) for root in roots):
            raise IntegrityError("manifest file is outside declared roots")
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise IntegrityError("invalid file digest")
        previous = path
    validate_entrypoint(manifest["invocation"], manifest)
    _check_deadline(deadline)


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _file_metadata(metadata):
    return _identity(metadata), metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


class _Snapshot:
    """Retain parent descriptors through each traversal and its identity checks."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.total_bytes = 0
        self.files = []

    def open_component(self, parent_fd, name, stack, *, directory=False):
        _check_deadline(self.deadline)
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        is_directory = stat.S_ISDIR(before.st_mode)
        if (directory and not is_directory) or not (is_directory or stat.S_ISREG(before.st_mode)):
            raise IntegrityError("symlink or non-regular input path")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        flags |= os.O_DIRECTORY if is_directory else os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=parent_fd)
        stack.callback(os.close, fd)
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            raise IntegrityError("input identity changed before open")
        _check_deadline(self.deadline)
        return fd, after

    def recheck_binding(self, parent_fd, name, expected):
        _check_deadline(self.deadline)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(expected):
            raise IntegrityError("input path binding changed during snapshot")

    def walk(self, fd, before, path):
        _check_deadline(self.deadline)
        if stat.S_ISREG(before.st_mode):
            if len(self.files) >= MAX_FILES:
                raise IntegrityError("snapshot file limit exceeded")
            if self.total_bytes + before.st_size > MAX_BYTES:
                raise IntegrityError("snapshot byte limit exceeded")
            digest = hashlib.sha256()
            while True:
                _check_deadline(self.deadline)
                block = os.read(fd, min(_CHUNK_BYTES, MAX_BYTES - self.total_bytes + 1))
                _check_deadline(self.deadline)
                if not block:
                    break
                self.total_bytes += len(block)
                if self.total_bytes > MAX_BYTES:
                    raise IntegrityError("snapshot byte limit exceeded")
                digest.update(block)
            if _file_metadata(before) != _file_metadata(os.fstat(fd)):
                raise IntegrityError("file changed during snapshot")
            self.files.append({"path": path, "sha256": digest.hexdigest()})
            return

        names = sorted(os.listdir(fd))
        _check_deadline(self.deadline)
        for name in names:
            with ExitStack() as child_stack:
                child_fd, child_stat = self.open_component(fd, name, child_stack)
                self.walk(child_fd, child_stat, path + "/" + name)
                self.recheck_binding(fd, name, child_stat)
        if names != sorted(os.listdir(fd)) or _file_metadata(before) != _file_metadata(os.fstat(fd)):
            raise IntegrityError("directory changed during snapshot")
        _check_deadline(self.deadline)

    def root(self, anchor_fd, path):
        components = path[1:].split("/")
        with ExitStack() as stack:
            current_fd = anchor_fd
            bindings = []
            for index, name in enumerate(components):
                child_fd, metadata = self.open_component(
                    current_fd, name, stack, directory=index < len(components) - 1,
                )
                bindings.append((current_fd, name, metadata))
                current_fd = child_fd
            self.walk(current_fd, metadata, path)
            for parent_fd, name, metadata in reversed(bindings):
                self.recheck_binding(parent_fd, name, metadata)


def snapshot(roots: list[str], *, deadline: float) -> list[dict[str, str]]:
    _check_deadline(deadline)
    ordered_roots = _validate_roots(roots, deadline)
    walker = _Snapshot(deadline)
    try:
        with ExitStack() as stack:
            anchor_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, anchor_fd)
            for root in ordered_roots:
                walker.root(anchor_fd, root)
    except OSError as exc:
        raise IntegrityError("unable to read declared input safely") from exc
    if not walker.files:
        raise IntegrityError("empty snapshot")
    _check_deadline(deadline)
    return sorted(walker.files, key=lambda entry: entry["path"])


def prepare_manifest(request: dict[str, Any], *, deadline: float | None = None) -> Manifest:
    deadline = time.monotonic() + PREPARE_SECONDS if deadline is None else deadline
    _check_deadline(deadline)
    _exact_fields(request, {"invocation", "roots"}, "request")
    _validate_invocation(request["invocation"])
    roots = _validate_roots(request["roots"], deadline)
    candidate = {"version": 1, "invocation": dict(request["invocation"]), "roots": roots,
                 "files": snapshot(roots, deadline=deadline)}
    validate_manifest_schema(candidate, deadline=deadline)
    return candidate


def verify_manifest(manifest: Manifest, *, deadline: float | None = None) -> None:
    deadline = time.monotonic() + CHECK_SECONDS if deadline is None else deadline
    validate_manifest_schema(manifest, deadline=deadline)
    if snapshot(manifest["roots"], deadline=deadline) != manifest["files"]:
        raise IntegrityError("declared input contents or membership changed")
    _check_deadline(deadline)
