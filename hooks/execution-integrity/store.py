"""Private offline candidate/receipt store; receipts are not execution permission."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import secrets
import stat
import time

from manifest import (
    CHECK_SECONDS, canonical_bytes, invocation_key, load_json, manifest_digest,
    validate_manifest_schema, verify_manifest,
)
from policy import IntegrityError, Invocation, Manifest, validate_absolute_path

_HEX = re.compile(r"[0-9a-f]{64}")
_RECEIPT_FIELDS = {"version", "digest", "invocation_key", "active", "source_session", "updated_at"}


def _deadline(value):
    value = time.monotonic() + CHECK_SECONDS if value is None else value
    _check(value)
    return value


def _check(deadline):
    if time.monotonic() >= deadline:
        raise IntegrityError("store deadline exceeded")


def _digest(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise IntegrityError("invalid store digest")


def _source(value):
    if not isinstance(value, str) or not value.strip():
        raise IntegrityError("missing approval source context")


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _private(metadata, mode):
    if stat.S_IMODE(metadata.st_mode) != mode or metadata.st_uid != os.getuid():
        raise IntegrityError("store path is not private and owned")


def _directory(parent, name, stack, bindings, deadline, *, create, private):
    _check(deadline)
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise IntegrityError("symlink or non-directory store path")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    stack.callback(os.close, fd)
    after = os.fstat(fd)
    if _identity(before) != _identity(after):
        raise IntegrityError("store directory identity changed")
    if private:
        _private(after, 0o700)
    bindings.append((parent, name, after))
    _check(deadline)
    return fd


def _check_files(directory, deadline):
    for name in os.listdir(directory):
        _check(deadline)
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            # A concurrent publisher may remove its non-authoritative name.
            if name.startswith(".tmp-"):
                continue
            raise
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrityError("symlink or non-regular store path")
        _private(metadata, 0o600)


@contextmanager
def _layout(state_dir: Path, deadline, *, create=False):
    """Retain every no-follow parent descriptor through the whole operation."""
    validate_absolute_path(str(state_dir))
    if str(state_dir) == "/":
        raise IntegrityError("unsupported state directory")
    try:
        with ExitStack() as stack:
            root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, root)
            bindings = []
            current = root
            parts = str(state_dir)[1:].split("/")
            for index, name in enumerate(parts):
                current = _directory(current, name, stack, bindings, deadline,
                                     create=create, private=index == len(parts) - 1)
            candidates = _directory(current, "candidates", stack, bindings, deadline,
                                    create=create, private=True)
            receipts = _directory(current, "receipts", stack, bindings, deadline,
                                  create=create, private=True)
            lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if create:
                try:
                    lock = os.open("store.lock", lock_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=current)
                    stack.callback(os.close, lock)
                    os.fsync(lock)
                    os.fsync(current)
                except FileExistsError:
                    lock = os.open("store.lock", lock_flags, dir_fd=current)
                    stack.callback(os.close, lock)
            else:
                lock = os.open("store.lock", lock_flags, dir_fd=current)
                stack.callback(os.close, lock)
            metadata = os.fstat(lock)
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError("non-regular store lock")
            _private(metadata, 0o600)
            bindings.append((current, "store.lock", metadata))
            _check_files(candidates, deadline)
            _check_files(receipts, deadline)
            _check(deadline)
            yield candidates, receipts, lock
            for parent, name, expected in reversed(bindings):
                _check(deadline)
                actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if _identity(actual) != _identity(expected):
                    raise IntegrityError("store path binding changed")
    except OSError as exc:
        raise IntegrityError("unable to access private store safely") from exc


@contextmanager
def _locked(fd, deadline):
    while True:
        _check(deadline)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    try:
        _check(deadline)
        yield
        _check(deadline)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read(directory, name, deadline):
    _check(deadline)
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise IntegrityError("symlink or non-regular store file")
    _private(before, 0o600)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        metadata = os.fstat(fd)
        if _identity(before) != _identity(metadata):
            raise IntegrityError("store file identity changed")
        chunks = []
        while True:
            _check(deadline)
            block = os.read(fd, 65536)
            _check(deadline)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise IntegrityError("store file changed while reading")
        if _identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _identity(metadata):
            raise IntegrityError("store file binding changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _candidate(directory, digest, deadline):
    _digest(digest)
    value = load_json(_read(directory, f"{digest}.json", deadline))
    validate_manifest_schema(value, deadline=deadline)
    if manifest_digest(value) != digest:
        raise IntegrityError("candidate digest mismatch")
    _check(deadline)
    return value


def _private_write(directory, raw, deadline):
    """Unbuffered writes: fsync and close before any authoritative publication."""
    name = ".tmp-" + secrets.token_hex(16)
    _check(deadline)
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        remaining = memoryview(raw)
        while remaining:
            _check(deadline)
            written = os.write(fd, remaining)
            if written <= 0:
                raise IntegrityError("incomplete private store write")
            remaining = remaining[written:]
        _check(deadline)
        os.fsync(fd)
        _check(deadline)
    except BaseException:
        os.close(fd)
        os.unlink(name, dir_fd=directory)
        raise
    else:
        os.close(fd)
    return name


def _replace_receipt(directory, receipt, deadline):
    name = _private_write(directory, canonical_bytes(receipt), deadline)
    try:
        _check(deadline)
        target = f"{receipt['digest']}.json"
        try:
            metadata = os.stat(target, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrityError("non-regular receipt replacement path")
            _private(metadata, 0o600)
        os.replace(name, target, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        _check(deadline)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _receipts(candidates, receipts, deadline):
    result = []
    for name in sorted(os.listdir(receipts)):
        _check(deadline)
        # A crashed private write is not authoritative state.
        if name.startswith(".tmp-"):
            continue
        if not name.endswith(".json"):
            raise IntegrityError("unknown receipt filename")
        digest = name[:-5]
        _digest(digest)
        receipt = load_json(_read(receipts, name, deadline))
        if set(receipt) != _RECEIPT_FIELDS or type(receipt["version"]) is not int or receipt["version"] != 1:
            raise IntegrityError("invalid receipt schema")
        if receipt["digest"] != digest or type(receipt["active"]) is not bool:
            raise IntegrityError("invalid receipt binding")
        _source(receipt["source_session"])
        if not isinstance(receipt["updated_at"], str) or not receipt["updated_at"]:
            raise IntegrityError("invalid receipt timestamp")
        candidate = _candidate(candidates, digest, deadline)
        if receipt["invocation_key"] != invocation_key(candidate["invocation"]):
            raise IntegrityError("receipt invocation mismatch")
        result.append((receipt, candidate))
    _check(deadline)
    return result


def publish_candidate(state_dir: Path, candidate: Manifest, *, deadline: float | None = None) -> str:
    deadline = _deadline(deadline)
    validate_manifest_schema(candidate, deadline=deadline)
    raw = canonical_bytes(candidate)
    digest = manifest_digest(candidate)
    _check(deadline)
    with _layout(state_dir, deadline, create=True) as (candidates, _receipts_dir, _lock):
        name = _private_write(candidates, raw, deadline)
        try:
            _check(deadline)
            try:
                os.link(name, f"{digest}.json", src_dir_fd=candidates, dst_dir_fd=candidates,
                        follow_symlinks=False)
            except FileExistsError:
                existing = _candidate(candidates, digest, deadline)
                if canonical_bytes(existing) != raw:
                    raise IntegrityError("conflicting candidate publication")
        finally:
            os.unlink(name, dir_fd=candidates)
            os.fsync(candidates)
        _check(deadline)
    return digest


def approve(state_dir: Path, digest: str, source_session: str, *, deadline: float | None = None) -> None:
    deadline = _deadline(deadline)
    _digest(digest)
    _source(source_session)
    with _layout(state_dir, deadline) as (candidates, receipts, lock), _locked(lock, deadline):
        candidate = _candidate(candidates, digest, deadline)
        verify_manifest(candidate, deadline=deadline)
        previous = _receipts(candidates, receipts, deadline)
        key = invocation_key(candidate["invocation"])
        timestamp = datetime.now(timezone.utc).isoformat()
        # Deactivate first. A crash before the replacement leaves zero, never a guessed winner.
        for receipt, _candidate_value in previous:
            if receipt["active"] and receipt["invocation_key"] == key and receipt["digest"] != digest:
                _replace_receipt(receipts, receipt | {"active": False, "updated_at": timestamp}, deadline)
        receipt = {"version": 1, "digest": digest, "invocation_key": key, "active": True,
                   "source_session": source_session, "updated_at": timestamp}
        _replace_receipt(receipts, receipt, deadline)


def revoke(state_dir: Path, digest: str, source_session: str, *, deadline: float | None = None) -> None:
    deadline = _deadline(deadline)
    _digest(digest)
    _source(source_session)
    with _layout(state_dir, deadline) as (candidates, receipts, lock), _locked(lock, deadline):
        matches = [receipt for receipt, _candidate_value in _receipts(candidates, receipts, deadline)
                   if receipt["digest"] == digest]
        if len(matches) != 1:
            raise IntegrityError("missing revocation receipt")
        _replace_receipt(receipts, matches[0] | {"active": False, "source_session": source_session,
                                              "updated_at": datetime.now(timezone.utc).isoformat()}, deadline)


def approved_candidate(state_dir: Path, invocation: Invocation, *, deadline: float | None = None) -> Manifest:
    """Return the unique bound baseline; the execution checker must still resnapshot."""
    deadline = _deadline(deadline)
    key = invocation_key(invocation)
    with _layout(state_dir, deadline) as (candidates, receipts, lock), _locked(lock, deadline):
        matches = [candidate for receipt, candidate in _receipts(candidates, receipts, deadline)
                   if receipt["active"] and receipt["invocation_key"] == key]
        if len(matches) != 1 or matches[0]["invocation"] != invocation:
            raise IntegrityError("missing or ambiguous active approval")
        return matches[0]
